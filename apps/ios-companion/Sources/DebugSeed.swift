#if DEBUG
import Foundation

/// Simulator seeds, set with SIMCTL_CHILD_ environment variables (the
/// simulator has no microphone to talk into and no Messages automation):
///   JOAT_DEBUG_SEED=1      an opened note gets a long transcript and two AI cues
///   JOAT_DEBUG_ANSWERS=a|b each question on the glasses is answered in turn
///   JOAT_DEBUG_TEXTS=1     three texts arrive: a contact, a spam, a group
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
    }

    static func noteOpened(_ notes: QuickNotes, cues: Cues?) {
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
