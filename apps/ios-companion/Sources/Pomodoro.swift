import Foundation

/// The Pomodoro and the one-shot countdown timer: a port of
/// `apps/mac/joat/services/pomodoro.py` (state machine, rows, wording) and
/// `persistence/pomodoro.py` (the checkpoint, defaults). The plugin counts
/// down locally from `endsAtUnixMs`; this side only ends phases and pushes.
final class Pomodoro: PanelProvider {
    let tab = "pomodoro"
    let statusRows = 0

    // persistence/pomodoro.py defaults and limits
    static let defaultFocusMs = 25 * 60_000
    static let defaultShortBreakMs = 5 * 60_000
    static let defaultLongBreakMs = 15 * 60_000
    static let defaultBlocksPerSet = 4
    static let maxBlocksPerSet = 12
    static let maxLengthMin = 24 * 60
    // pomodoro.py presets
    static let lengthPresets: [String: [Int]] = [
        "focus": [15, 20, 25, 30, 45, 50, 60],
        "short_break": [3, 5, 10, 15],
        "long_break": [10, 15, 20, 30],
    ]
    static let sessionPresets = [2, 3, 4, 5, 6, 8]
    static let quickTimerMin = 10
    static let rowLabels = ["focus": "Focus", "short_break": "Short break", "long_break": "Long break"]
    static let receiptNouns = ["focus": "Focus blocks", "short_break": "Short breaks", "long_break": "Long breaks"]

    /// The one truth, persisted on every change (PomodoroCheckpoint).
    struct Checkpoint: Codable {
        var phase = "idle"          // idle | focus | break | long_break | timer
        var running = false
        var remainingMs = Pomodoro.defaultFocusMs
        var lengthMs = Pomodoro.defaultFocusMs
        var completed = 0
        var endsAtUnixMs: Int? = nil
        var savedAtMs = 0
        var focusMs = Pomodoro.defaultFocusMs
        var shortBreakMs = Pomodoro.defaultShortBreakMs
        var longBreakMs = Pomodoro.defaultLongBreakMs
        var blocksPerSet = Pomodoro.defaultBlocksPerSet

        func length(of kind: String) -> Int {
            switch kind {
            case "focus": return focusMs
            case "short_break": return shortBreakMs
            default: return longBreakMs
            }
        }
        func length(forPhase phase: String) -> Int {
            switch phase {
            case "long_break": return longBreakMs
            case "break": return shortBreakMs
            default: return focusMs
            }
        }
    }

    private static let storeKey = "joat.pomodoro.checkpoint.v1"
    private var c: Checkpoint
    private var timer: DispatchSourceTimer?
    private var recoveryNotice: String?
    /// Push the tab (PanelPublisher.publish("pomodoro")) after every change.
    var onChange: (() -> Void)?
    /// A phase-end card on the lens (session.notify → Hud.card).
    var onCard: ((_ source: String, _ summary: String) -> Void)?

    init() {
        if let data = UserDefaults.standard.data(forKey: Self.storeKey),
           let saved = try? JSONDecoder().decode(Checkpoint.self, from: data) {
            c = saved
        } else {
            c = Checkpoint()
        }
        restoreAfterAbsence()
        arm()
    }

    private static func nowMs() -> Int { Int((Date().timeIntervalSince1970 * 1000).rounded()) }
    private static func minutes(_ ms: Int) -> Int { ms / 60_000 }
    private static func minutesPhrase(_ ms: Int) -> String {
        let m = minutes(ms)
        return "\(m) minute\(m != 1 ? "s" : "")"
    }

    // MARK: Observation

    func remainingMs() -> Int {
        if c.running, let end = c.endsAtUnixMs {
            return min(c.lengthMs, max(0, end - Self.nowMs()))
        }
        return c.remainingMs
    }

    var phase: String { c.phase }

    /// HudPanelPomodoro.to_wire
    func wireState() -> [String: Any] {
        var w: [String: Any] = [
            "phase": c.phase, "running": c.running,
            "remainingMs": min(remainingMs(), c.lengthMs), "lengthMs": c.lengthMs,
            "completed": c.completed, "blocksPerSet": c.blocksPerSet,
        ]
        if c.running, let end = c.endsAtUnixMs { w["endsAtUnixMs"] = end }
        return w
    }

    private func controls() -> [(String, () -> String)] {
        if c.phase == "idle" {
            var rows: [(String, () -> String)] = [("▶ Start a focus block", { self.start() })]
            for kind in ["focus", "short_break", "long_break"] {
                rows.append(("\(Self.rowLabels[kind]!): \(Self.minutes(c.length(of: kind))) min", { self.cycleLength(kind) }))
            }
            rows.append(("Sessions: \(c.blocksPerSet)", { self.cycleSessions() }))
            rows.append(("▶ \(Self.quickTimerMin) min timer", { self.startTimer(minutes: Self.quickTimerMin) }))
            return rows
        }
        if !c.running { return [("▶ Resume", { self.resume() }), ("Reset", { self.reset() })] }
        if c.phase == "timer" { return [("Pause", { self.pause() }), ("Cancel timer", { self.reset() })] }
        let skip = c.phase == "focus" ? "Skip to break" : "Skip break"
        return [("Pause", { self.pause() }), (skip, { self.skip() }), ("Reset", { self.reset() })]
    }

    func statusText() -> String {
        let prefix = recoveryNotice.map { "\($0) " } ?? ""
        let blocks = c.blocksPerSet
        if c.phase == "idle" {
            return "No pomodoro running. A focus block is \(Self.minutesPhrase(c.focusMs)), \(blocks) to a set."
        }
        let left = max(0, Int((Double(remainingMs()) / 60_000).rounded(.up)))
        let what: String
        switch c.phase {
        case "focus": what = "focus block \(min(blocks, c.completed + 1)) of \(blocks)"
        case "break": what = "short break"
        case "long_break": what = "long break"
        case "timer": what = "timer"
        default: what = c.phase
        }
        let state = c.running ? "" : "Paused. "
        return "\(prefix)\(state)\(left) minute\(left != 1 ? "s" : "") left in your \(what)."
    }

    // MARK: Mutation (the Mac's wording, verbatim)

    @discardableResult func start() -> String {
        if c.phase == "idle" {
            commit(phaseCheckpoint("focus", completed: c.completed, running: true))
            return "Focus block \(c.completed + 1) of \(c.blocksPerSet). \(Self.minutesPhrase(c.focusMs))."
        }
        if !c.running { return resume() }
        return "The timer is already running. " + statusText()
    }

    @discardableResult func startTimer(minutes: Int) -> String {
        guard (1...Self.maxLengthMin).contains(minutes) else { return "A timer is 1 to \(Self.maxLengthMin) minutes." }
        if c.phase != "idle" {
            let what = c.phase == "timer" ? "A timer" : "A pomodoro"
            return "\(what) is already under way. Say reset first. " + statusText()
        }
        let length = minutes * 60_000
        commit(phaseCheckpoint("timer", completed: 0, running: true, lengthMs: length))
        return "Timer set for \(Self.minutesPhrase(length))."
    }

    @discardableResult func pause() -> String {
        if c.phase == "idle" || !c.running { return "Nothing is running." }
        var n = c
        n.running = false
        n.remainingMs = remainingMs()
        n.endsAtUnixMs = nil
        commit(n)
        return statusText()
    }

    @discardableResult func resume() -> String {
        if c.phase == "idle" { return start() }
        if c.running { return "It is already running." }
        var n = c
        n.running = true
        n.endsAtUnixMs = Self.nowMs() + c.remainingMs
        commit(n)
        return "Resumed. " + statusText()
    }

    @discardableResult func skip() -> String {
        if c.phase == "idle" { return start() }
        advance(skipped: true)
        return statusText()
    }

    @discardableResult func reset() -> String {
        commit(phaseCheckpoint("idle", completed: 0, running: false))
        return "Pomodoro reset."
    }

    @discardableResult func cycleLength(_ kind: String) -> String {
        let presets = Self.lengthPresets[kind] ?? []
        let current = c.length(of: kind)
        let minutes = presets.first { $0 * 60_000 > current } ?? presets.first ?? 25
        return applyLength(kind, minutes: minutes)
    }

    @discardableResult func setLength(_ kind: String, minutes: Int) -> String {
        guard (1...Self.maxLengthMin).contains(minutes) else { return "A length is 1 to \(Self.maxLengthMin) minutes." }
        return applyLength(kind, minutes: minutes)
    }

    private func applyLength(_ kind: String, minutes: Int) -> String {
        var n = c
        switch kind {
        case "focus": n.focusMs = minutes * 60_000
        case "short_break": n.shortBreakMs = minutes * 60_000
        default: n.longBreakMs = minutes * 60_000
        }
        applyLengths(n)
        return "\(Self.receiptNouns[kind]!): \(Self.minutesPhrase(n.length(of: kind)))."
    }

    @discardableResult func setSessions(_ count: Int) -> String {
        guard (1...Self.maxBlocksPerSet).contains(count) else { return "A set is 1 to \(Self.maxBlocksPerSet) sessions." }
        guard sessionsFit(count) else {
            return "You are \(c.completed) blocks into this set. Say reset, then set \(count) sessions."
        }
        var n = c
        n.blocksPerSet = count
        applyLengths(n)
        return "\(count) session\(count != 1 ? "s" : "") to a set."
    }

    @discardableResult func cycleSessions() -> String {
        setSessions(Self.sessionPresets.first { $0 > c.blocksPerSet } ?? Self.sessionPresets[0])
    }

    /// Idle, the page's countdown follows the focus length; during a phase
    /// the block under way keeps the length it started with.
    private func applyLengths(_ lengths: Checkpoint) {
        if c.phase == "idle" {
            var n = phaseCheckpoint("idle", completed: 0, running: false, lengths: lengths)
            n.blocksPerSet = lengths.blocksPerSet
            commit(n)
        } else {
            var n = c
            n.focusMs = lengths.focusMs
            n.shortBreakMs = lengths.shortBreakMs
            n.longBreakMs = lengths.longBreakMs
            n.blocksPerSet = lengths.blocksPerSet
            n.remainingMs = remainingMs()
            commit(n)
        }
    }

    private func sessionsFit(_ count: Int) -> Bool {
        switch c.phase {
        case "idle", "timer": return true
        case "focus": return c.completed < count
        case "break": return 1 <= c.completed && c.completed < count
        default: return c.completed == count
        }
    }

    // MARK: PanelProvider

    func snapshot() -> PanelSnapshot {
        PanelSnapshot(rows: controls().map { $0.0 }, pomodoro: wireState())
    }
    func refresh(completion: @escaping () -> Void) { completion() }
    func open(index: Int, completion: @escaping (Bool) -> Void) {
        let rows = controls()
        if rows.indices.contains(index) { _ = rows[index].1() }
        completion(false)
    }

    // MARK: Internals

    private func phaseCheckpoint(_ phase: String, completed: Int, running: Bool,
                                 lengths: Checkpoint? = nil, lengthMs: Int? = nil) -> Checkpoint {
        var n = lengths ?? c
        let length = lengthMs ?? n.length(forPhase: phase)
        let now = Self.nowMs()
        n.phase = phase
        n.running = running
        n.remainingMs = length
        n.lengthMs = length
        n.completed = completed
        n.endsAtUnixMs = running ? now + length : nil
        n.savedAtMs = now
        return n
    }

    private func commit(_ n: Checkpoint) {
        c = n
        c.savedAtMs = Self.nowMs()
        recoveryNotice = nil
        if let data = try? JSONEncoder().encode(c) { UserDefaults.standard.set(data, forKey: Self.storeKey) }
        arm()
        onChange?()
    }

    private func nextPhase() -> (String, Int) {
        if c.phase == "timer" { return ("idle", 0) }
        if c.phase == "focus" {
            let done = c.completed + 1
            return (done >= c.blocksPerSet ? "long_break" : "break", done)
        }
        return ("focus", c.phase == "long_break" ? 0 : c.completed)
    }

    private func advance(skipped: Bool = false) {
        let finished = c.phase
        let finishedMs = c.lengthMs
        let (next, completed) = nextPhase()
        commit(phaseCheckpoint(next, completed: completed, running: next != "idle"))
        if finished == "timer" {
            onCard?(skipped ? "Timer ended" : "Timer done",
                    "\(Self.minutesPhrase(finishedMs)) timer " + (skipped ? "ended early" : "finished"))
        } else {
            announce(next: next, skipped: skipped)
        }
    }

    private func announce(next: String, skipped: Bool) {
        let minutes = c.lengthMs / 60_000
        let headline: String
        let summary: String
        if next == "focus" {
            summary = "Focus block \(c.completed + 1) of \(c.blocksPerSet) — \(minutes) min"
            headline = skipped ? "Break skipped" : "Break over"
        } else if next == "long_break" {
            let b = c.blocksPerSet
            summary = "\(b) block\(b != 1 ? "s" : "") done — long break, \(minutes) min"
            headline = "Focus done"
        } else {
            summary = "Break for \(minutes) min"
            headline = skipped ? "Focus skipped" : "Focus done"
        }
        onCard?(headline, summary)
    }

    /// The phase end, on the main queue. JOAT Brain stays alive in the
    /// background on its audio session, so this fires with the phone locked.
    private func arm() {
        timer?.cancel()
        timer = nil
        guard c.running, let end = c.endsAtUnixMs else { return }
        let delay = max(0, Double(end - Self.nowMs()) / 1000)
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + delay)
        t.setEventHandler { [weak self] in
            guard let self, self.c.running, let end = self.c.endsAtUnixMs else { return }
            if Self.nowMs() < end { self.arm(); return }
            self.advance()
        }
        t.resume()
        timer = t
    }

    /// pomodoro.py activate_restored: a deadline that passed while JOAT Brain
    /// was not running is reported, not silently replayed.
    private func restoreAfterAbsence() {
        guard c.running, let end = c.endsAtUnixMs, end <= Self.nowMs() else { return }
        if c.phase == "timer" {
            c = phaseCheckpointFrom(c, phase: "idle", completed: 0, running: false)
            recoveryNotice = "Your timer finished while JOAT was away."
        } else {
            let label = ["focus": "Focus", "break": "Break", "long_break": "Long break"][c.phase] ?? "Focus"
            let (next, completed) = nextPhase()
            c = phaseCheckpointFrom(c, phase: next, completed: completed, running: false)
            let nextLabel = ["focus": "Focus", "break": "Short break", "long_break": "Long break"][next] ?? next
            recoveryNotice = "\(label) deadline passed while JOAT was away. \(nextLabel) is paused."
        }
        if let data = try? JSONEncoder().encode(c) { UserDefaults.standard.set(data, forKey: Self.storeKey) }
        recoveryCardPending = true
    }

    private var recoveryCardPending = false

    /// The Mac's "Timer recovered" card, shown once a plugin is listening.
    func flushRecoveryCard() {
        guard recoveryCardPending, let notice = recoveryNotice else { return }
        recoveryCardPending = false
        onCard?("Timer recovered", notice)
    }

    private func phaseCheckpointFrom(_ base: Checkpoint, phase: String, completed: Int, running: Bool) -> Checkpoint {
        var n = base
        let length = n.length(forPhase: phase)
        n.phase = phase; n.running = running; n.remainingMs = length; n.lengthMs = length
        n.completed = completed; n.endsAtUnixMs = nil; n.savedAtMs = Self.nowMs()
        return n
    }
}
