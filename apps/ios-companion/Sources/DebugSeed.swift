#if DEBUG
import Foundation

/// Simulator seeds, set with SIMCTL_CHILD_ environment variables (the
/// simulator has no microphone to talk into and no Messages automation):
///   JOAT_DEBUG_SEED=1      an opened note gets a long transcript and two AI cues
///   JOAT_DEBUG_ANSWERS=a|b each question on the glasses is answered in turn
///   JOAT_DEBUG_TEXTS=1     four texts arrive: a contact, a spam, two groups
///   JOAT_DEBUG_OPEN=Name   that page opens, as asking for it by voice would
///   JOAT_DEBUG_PAGE=1      a notebook page: ink, typing, Done, Claude reads it
///   JOAT_UITEST=1          the notebook UI test's pictures (dist/sim-cues-1030/uitest)
enum DebugSeed {
    private static let env = ProcessInfo.processInfo.environment
    private static var answers: [String] = (env["JOAT_DEBUG_ANSWERS"] ?? "")
        .split(separator: "|").map(String.init)

    /// The seed answers the glasses' questions (JOAT_DEBUG_ANSWERS).
    static var answersQuestions: Bool { env["JOAT_DEBUG_ANSWERS"] != nil }

    static func launched(_ brain: Brain) {
        // JOAT_UITEST=1: the notebook UI test asks the app for pictures.
        if env["JOAT_UITEST"] == "1" { Task { @MainActor in Notebook.listenForSnaps() } }
        // JOAT_DEBUG_PAGE=1 (with -JOATTab notes): a notebook page, end to end.
        if env["JOAT_DEBUG_PAGE"] == "1" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { Notebook.debugRun() }
        }
        // JOAT_DEBUG_MAIL=1: synthetic multi-account Gmail inbox for Simulator UI checks.
        if env["JOAT_DEBUG_MAIL"] == "1" {
            Task { @MainActor in brain.mail.debugSeedSampleInbox() }
        }
        // JOAT_DEBUG_NAVIGATION_SEED=1: synthetic Saved Places + active route summary for Simulator UI checks.
        if env["JOAT_DEBUG_NAVIGATION_SEED"] == "1" {
            Task { @MainActor in brain.navigation.debugSeedSampleState() }
        }
        // JOAT_DEBUG_NOTEBOOK_SEED=1: pre-populates a ruled Apple Pencil page + voice note + prep folder.
        if env["JOAT_DEBUG_NOTEBOOK_SEED"] == "1" {
            Task { @MainActor in
                let pageId = "debug-duo-page-1"
                let drawing = Notebook.debugDrawing(
                    ["Call Mike about the tag map", "on Friday at 3 pm", "Order two spare relays"],
                    top: 140
                )
                PageStore.save(drawing, pageId)
                brain.quickNotes.ensureBlankPage(id: pageId, paperTemplate: PaperTemplate.ruled.rawValue)
                brain.quickNotes.savePage(
                    id: pageId,
                    typed: "Q4 hardware validation notes — verify G2 lens alignment and battery telemetry.",
                    inkChanged: false,
                    paperTemplate: PaperTemplate.ruled.rawValue
                )
                brain.quickNotes.renameNote(pageId, to: "Q4 Hardware Walkthrough")
                if JOATRuntime.shared.prep.notes.isEmpty {
                    let prepId = JOATRuntime.shared.prep.create()
                    JOATRuntime.shared.prep.addText(
                        "Agenda: Q4 launch checklist, firmware 0.1.152 sign-off, TestFlight rollout.",
                        title: "Launch Brief",
                        source: "Briefing",
                        to: prepId
                    )
                }
                JOATRuntime.shared.notes.reload()
                if env["JOAT_DEBUG_OPEN_EDITOR"] == "1" {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                        NotificationCenter.default.post(name: Notebook.debugOpen, object: pageId)
                    }
                }
            }
        }
        // JOAT_DEBUG_OPEN=Messages: the page opens as "show my messages" would.
        if let name = env["JOAT_DEBUG_OPEN"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 16) { brain.capabilities.openByVoice(name) }
        }
        guard env["JOAT_DEBUG_TEXTS"] == "1" else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            brain.messages.arrived(from: "Honey Bun", body: "Are you picking up the kids today?")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
            brain.messages.arrived(from: "+1 (405) 555-0199",
                                   body: "USPS: your package is on hold. Update your address now at usps-redelivery-help.top to avoid return.")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) {
            brain.messages.arrived(from: "Family (Honey Bun)", body: "Dinner at 7 at Mom's, bring the dessert.")
        }
        // The one-action shortcut (2026-11-12): the raw sender and the group
        // on its own; JOAT Brain finds the name in Contacts.
        DispatchQueue.main.asyncAfter(deadline: .now() + 14) {
            brain.messages.arrived(from: "+14055550123", body: "Running late, start without me.", group: "Book Club")
        }
        // The text-first shortcut (2026-11-14): the text, then its group
        // from "Add Group to Text" a second later.
        DispatchQueue.main.asyncAfter(deadline: .now() + 16) {
            brain.messages.arrived(from: "+14055550123", body: "Bringing the pie tonight.")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 17) {
            brain.messages.conversation(group: "Fam Jam")
        }
        // Find Conversation failed: no group step. The text still shows.
        DispatchQueue.main.asyncAfter(deadline: .now() + 21) {
            brain.messages.arrived(from: "Honey Bun", body: "Call me when you can.")
        }
    }

    static func noteOpened(_ notes: QuickNotes, cues: Cues?) {
        // JOAT_DEBUG_GROW=1: a meeting's worth of words, a paragraph every
        // 2 s while the note is open (the simulator has no dictation).
        if env["JOAT_DEBUG_GROW"] == "1" {
            var n = 0
            Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { timer in
                guard notes.active else { timer.invalidate(); return }
                n += 1
                notes.debugAppend(["Meeting paragraph \(n): we went over the schedule, the open items on the Madison build, who owns the tag map review, and what needs to be ready before Friday's walkthrough with the client."])
            }
        }
        guard env["JOAT_DEBUG_SEED"] == "1" else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            notes.debugAppend((1...14).map {
                "Paragraph \($0): we walked through the historian cutover, the tag paths and who signs off on the MQTT bridge."
            })
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            cues?.debugCue(Cues.Cue(kind: "ANSWER", title: "MQTT bridge date",
                                    body: "Prep says IT firewall approval comes first; the earliest is Nov 6.", at: Date()))
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 45) {
            cues?.debugCue(Cues.Cue(kind: "CONCEPT", title: "Historian",
                                    body: "A database that stores plant tag values over time, for trends and reports.", at: Date()))
        }
    }

    static func asked(_ prompts: Prompts) {
        guard !answers.isEmpty else { return }
        let answer = answers.removeFirst()
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            Log.add("debug: answering “\(answer)”")
            _ = prompts.heard(answer)
        }
    }
}
#endif
