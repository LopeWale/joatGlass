import AppKit
import ApplicationServices
import ServiceManagement
import SwiftUI
import UniformTypeIdentifiers

/// Owns the Python engine as a child process (docs/MAC_APP.md §Native shell).
///
/// The engine inherits this app's TCC identity, so the single Full Disk
/// Access grant on JOAT.app covers notification reading. Quit stops the
/// engine — a closed app means nothing is listening (privacy semantics
/// mirror doff-stops-mic).
@MainActor
final class EngineController: ObservableObject {
    static let shared = EngineController()

    enum State: Equatable { case stopped, running, crashed(Int32), failed(String) }
    private enum LaunchMode { case normal, brainstormAcceptance }

    /// Single source of truth for the engine's WS port — passed to the
    /// engine via --port and used by the stale-engine reaper. Overridable
    /// without a rebuild: `defaults write com.joat.shell EnginePort 9090`
    /// (UserDefaults, not an env var — GUI apps launched from Finder or as
    /// login items don't inherit the shell environment). NOTE: the hub
    /// plugin's VITE_MAC_WS_URL and the packed app.json whitelist must
    /// still agree with whatever this resolves to.
    nonisolated static func defaultEnginePort(bundleIdentifier: String?) -> Int {
        bundleIdentifier == "com.joat.shell.debug" ? 18_787 : 8_787
    }

    nonisolated static func defaultSttPort(bundleIdentifier: String?) -> Int {
        bundleIdentifier == "com.joat.shell.debug" ? 51_060 : 50_060
    }

    /// Hosted XCTest launches the application target to inject its bundle.
    /// Starting the real notification watchers and Python engine from that
    /// short-lived host would request Files & Folders access for a disposable
    /// Debug identity and make its prompt look like the installed JOAT app.
    nonisolated static func shouldRunAppServices(
        environment: [String: String]
    ) -> Bool {
        environment["XCTestConfigurationFilePath"] == nil
    }

    static let enginePort =
        (UserDefaults.standard.object(forKey: "EnginePort") as? Int)
        ?? defaultEnginePort(bundleIdentifier: Bundle.main.bundleIdentifier)

    static let sttPort =
        (UserDefaults.standard.object(forKey: "SttPort") as? Int)
        ?? defaultSttPort(bundleIdentifier: Bundle.main.bundleIdentifier)

    @Published var state: State = .stopped
    @Published var launchAtLogin: Bool = SMAppService.mainApp.status == .enabled
    @Published var accessibilityTrusted = false
    @Published var pairingNotice: String?
    @Published var calendarAccess = "not determined"
    @Published var remindersAccess = "not determined"
    @Published var g2DiagnosticsEnabled: Bool
    @Published var g2DiagnosticNotice: String?

    private var process: Process?
    private var userStopped = false
    private var launchMode: LaunchMode = .normal
    private var recentCrashes: [Date] = []
    private var pendingStopCompletion: (() -> Void)?

    private let defaults = UserDefaults.standard
    private let g2DiagnosticsKey = "G2DiagnosticsEnabled"
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/joat/brain.log")

    var statusLine: String {
        switch state {
        case .running:
            launchMode == .brainstormAcceptance
                ? "JOAT · G2 Brainstorm acceptance running"
                : "JOAT · engine running"
        case .stopped: "JOAT · stopped"
        case .crashed(let code): "JOAT · engine crashed (\(code))"
        case .failed(let why): "JOAT · \(why)"
        }
    }

    init() {
        g2DiagnosticsEnabled = UserDefaults.standard.bool(forKey: "G2DiagnosticsEnabled")
        guard Self.shouldRunAppServices(
            environment: ProcessInfo.processInfo.environment
        ) else { return }
        accessibilityTrusted = AXIsProcessTrusted()
        do {
            try NativeBridgeServer.shared.start()
            refreshPersonalDataAccess()
        } catch {
            appendLog("[shell] native personal-data bridge unavailable: \(error.localizedDescription)")
        }
        start()
        // The AX watcher is started from applicationDidFinishLaunching, NOT
        // here: this initializer can run inside SwiftUI's App.init(), and
        // anything that touches AX before the runloop exists deadlocks.
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
            Task { @MainActor in
                self.accessibilityTrusted = AXIsProcessTrusted()
            }
        }
    }

    func requestAccessibility() {
        NotificationWatcher.shared.requestAccess()
    }

    func refreshPersonalDataAccess() {
        Task { @MainActor in
            let snapshot = await NativeBridgeServer.shared.authorizationSnapshot()
            calendarAccess = display(snapshot.calendar)
            remindersAccess = display(snapshot.reminders)
        }
    }

    func requestCalendarAccess() {
        requestPersonalDataAccess(entity: "calendar")
    }

    func requestRemindersAccess() {
        requestPersonalDataAccess(entity: "reminders")
    }

    private func requestPersonalDataAccess(entity: String) {
        Task { @MainActor in
            do {
                let snapshot = try await NativeBridgeServer.shared.requestFullAccess(entity: entity)
                calendarAccess = display(snapshot.calendar)
                remindersAccess = display(snapshot.reminders)
            } catch {
                appendLog("[shell] \(entity) permission request failed: \(error.localizedDescription)")
                refreshPersonalDataAccess()
            }
        }
    }

    private func display(_ status: NativeAuthorization) -> String {
        switch status {
        case .notDetermined: "not determined"
        case .restricted: "restricted"
        case .denied: "denied"
        case .writeOnly: "write only"
        case .fullAccess: "full access"
        case .unknown: "unknown"
        }
    }

    // MARK: repo discovery — user-chosen, never hardcoded

    /// The repo path lives in UserDefaults; candidates are only conventional
    /// locations relative to $HOME, valid on any Mac.
    func repoURL() -> URL? {
        if let stored = defaults.string(forKey: "RepoPath") {
            let url = URL(fileURLWithPath: stored)
            if isRepo(url) { return url }
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        for candidate in ["Developer/joatGlass", "joatGlass", "Projects/joatGlass"] {
            let url = home.appendingPathComponent(candidate)
            if isRepo(url) {
                defaults.set(url.path, forKey: "RepoPath")
                return url
            }
        }
        return nil
    }

    private func isRepo(_ url: URL) -> Bool {
        FileManager.default.isExecutableFile(
            atPath: url.appendingPathComponent(".venv/bin/python").path)
    }

    /// Why repoURL() returned nil — three distinct failures that previously
    /// all collapsed into an opaque crashed(-1): repo never chosen, repo
    /// moved/deleted, or repo present but its venv broken (the symlinked
    /// venv python dangles after a Homebrew/macOS Python upgrade).
    private func repoDiagnosis() -> String {
        guard let stored = defaults.string(forKey: "RepoPath") else {
            return "repo not set — use Set Repo Folder…"
        }
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: stored, isDirectory: &isDir),
           isDir.boolValue {
            return "venv broken in \(stored) — recreate: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
        }
        return "repo missing at \(stored) — use Set Repo Folder…"
    }

    /// Failures must be visible in Open Log, not just the menu bar.
    private func fail(_ reason: String) {
        state = .failed(reason)
        appendLog("[shell] \(reason)")
    }

    private func appendLog(_ line: String) {
        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: logURL.path
        )
        if let h = try? FileHandle(forWritingTo: logURL) {
            _ = try? h.seekToEnd()
            try? h.write(contentsOf: Data((line + "\n").utf8))
            try? h.close()
        }
    }

    func chooseRepo() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.message = "Select the joatGlass repository (must contain .venv)"
        if panel.runModal() == .OK, let url = panel.url {
            defaults.set(url.path, forKey: "RepoPath")
            restart()
        }
    }

    // MARK: engine lifecycle

    func start() {
        guard process == nil else { return }
        guard let repo = repoURL() else {
            fail(repoDiagnosis())
            chooseRepo()
            return
        }
        userStopped = false
        reapStaleEngines()

        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try? FileHandle(forWritingTo: logURL)
        _ = try? log?.seekToEnd()

        let p = Process()
        p.executableURL = repo.appendingPathComponent(".venv/bin/python")
        p.arguments = [
            "-u", "-m", "joat",
            "--port", String(Self.enginePort),
            "--stt-port", String(Self.sttPort),
        ]
            + (g2DiagnosticsEnabled ? ["--g2-diagnostics"] : [])
            + (launchMode == .brainstormAcceptance ? ["--g2-brainstorm-acceptance"] : [])
        p.currentDirectoryURL = repo
        // GUI apps do not inherit the login shell's PATH; without brew and
        // ~/.local the engine silently degrades (no whisperkit-cli, no codex).
        var env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:"
            + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        if NativeBridgeServer.shared.isRunning {
            env["JOAT_NATIVE_BRIDGE"] = NativeBridgeServer.shared.socketURL.path
        }
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in self?.engineExited(code: proc.terminationStatus) }
        }
        NativeBridgeServer.shared.authorizePeerProcessID(nil)
        do {
            try p.run()
            NativeBridgeServer.shared.authorizePeerProcessID(p.processIdentifier)
            process = p
            state = .running
        } catch {
            fail("engine spawn failed: \(error.localizedDescription)")
        }
    }

    func stop(completion: (() -> Void)? = nil) {
        userStopped = true
        guard let p = process else {
            state = .stopped
            completion?()
            return
        }
        pendingStopCompletion = completion
        p.terminate()
        // Escalate if the engine ignores SIGTERM (audit §C2).
        let pid = p.processIdentifier
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            if self?.process != nil { kill(pid, SIGKILL) }
        }
    }

    func restart() {
        launchMode = .normal
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.start() }
    }

    /// An explicit Mac-side opt-in.  The child remains the exact PID admitted
    /// by NativeBridgeServer, so the temporary G2 test never turns an
    /// arbitrary shell process into an OpenClaw credential holder.
    func runG2BrainstormAcceptance() {
        guard launchMode != .brainstormAcceptance else { return }
        launchMode = .brainstormAcceptance
        if process == nil {
            start()
            return
        }
        stop { [weak self] in self?.start() }
    }

    var isG2BrainstormAcceptanceRunning: Bool {
        state == .running && launchMode == .brainstormAcceptance
    }

    private func engineExited(code: Int32) {
        NativeBridgeServer.shared.authorizePeerProcessID(nil)
        process = nil
        if userStopped {
            state = .stopped
            pendingStopCompletion?()
            pendingStopCompletion = nil
            return
        }
        if launchMode == .brainstormAcceptance, code == 0 {
            // The Python acceptance controller only exits cleanly after it
            // has emitted its owner-only receipt.  Return to normal JOAT
            // rather than treating one completed field run as a crash.
            launchMode = .normal
            start()
            return
        }
        state = .crashed(code)
        // Supervision with a circuit breaker: restart unless it's crash-looping.
        recentCrashes.append(Date())
        recentCrashes.removeAll { $0 < Date().addingTimeInterval(-300) }
        if recentCrashes.count <= 5 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.start() }
        }
    }

    /// Exactly one engine may own this app identity's resolved port. An app
    /// relaunch (or a crashed shell) can leave an orphan holding that port —
    /// the new engine would then crash-loop on bind. Reap by PORT OWNERSHIP:
    /// production and Debug intentionally use different ports, so a broad
    /// process-name kill would let a test restart the installed engine. The
    /// kernel reports
    /// the engine's argv with the resolved interpreter path (not .venv/...),
    /// so command-pattern matching is unreliable; holding the port is not.
    ///
    /// The superseded com.joat.brain launchd agent (setup_brain.sh --service)
    /// has KeepAlive=true — killing its process by port would just make
    /// launchd resurrect it, forever fighting this app. Boot it out and
    /// remove its plist FIRST; setup_brain.sh can always recreate it.
    private func reapStaleEngines() {
        let reap = Process()
        reap.executableURL = URL(fileURLWithPath: "/bin/sh")
        reap.arguments = ["-c",
            "launchctl bootout gui/$(id -u)/com.joat.brain 2>/dev/null; "
            + "rm -f \"$HOME/Library/LaunchAgents/com.joat.brain.plist\"; "
            + "PIDS=$(lsof -ti tcp:\(Self.enginePort) -sTCP:LISTEN); "
            + "[ -n \"$PIDS\" ] && kill $PIDS && sleep 1; true"]
        try? reap.run()
        reap.waitUntilExit()
    }

    // MARK: conveniences

    /// Open the only path by which a remote peer may become trusted.
    ///
    /// The marker is local-user authority and expires after 120 seconds. The
    /// Python gateway fails closed when the token is absent or corrupt unless
    /// this marker exists, so deleting pairing.json can never authorize a peer.
    func openPairingWindow() {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/JOAT")
        do {
            let expiresAt = try PairingWindowStore(directory: directory).open()
            let time = expiresAt.formatted(date: .omitted, time: .shortened)
            pairingNotice = "Pairing open until \(time). Connect the intended glasses now."
            appendLog("pairing window opened locally for 120 seconds")
        } catch {
            pairingNotice = "Pairing stayed closed: \(error.localizedDescription)"
            appendLog("pairing window failed closed: \(error.localizedDescription)")
        }
    }

    /// Enable a bounded, content-free G2 support capture. Changing the mode
    /// restarts the child so the authenticated gateway can inform the plugin;
    /// ordinary HUD rendering remains on its production path throughout.
    func setG2DiagnosticsEnabled(_ enabled: Bool) {
        guard enabled != g2DiagnosticsEnabled else { return }
        if enabled {
            do {
                try G2DiagnosticExport.clear()
            } catch {
                g2DiagnosticNotice = "Trace stayed off: \(error.localizedDescription)"
                appendLog("G2 diagnostic trace stayed off: \(error.localizedDescription)")
                return
            }
        }
        defaults.set(enabled, forKey: g2DiagnosticsKey)
        g2DiagnosticsEnabled = enabled
        g2DiagnosticNotice = enabled
            ? "G2 diagnostic trace is on. The engine restarted for this support capture."
            : "G2 diagnostic trace is off. The redacted capture remains available to export."
        appendLog(enabled ? "G2 diagnostic trace enabled" : "G2 diagnostic trace disabled")
        if state == .running { restart() }
    }

    func exportG2Diagnostics() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "joat-g2-diagnostics-redacted.json"
        panel.allowedContentTypes = [.json]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        do {
            try G2DiagnosticExport.export(to: destination)
            g2DiagnosticNotice = "Redacted G2 trace exported to \(destination.lastPathComponent)."
            appendLog("redacted G2 diagnostic trace exported")
        } catch {
            g2DiagnosticNotice = "G2 trace export failed: \(error.localizedDescription)"
            appendLog("G2 diagnostic trace export failed: \(error.localizedDescription)")
        }
    }

    func openLog() {
        NSWorkspace.shared.open(logURL)
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            launchAtLogin = enabled
        } catch {
            launchAtLogin = SMAppService.mainApp.status == .enabled
            if SMAppService.mainApp.status == .requiresApproval {
                // The user disabled us in System Settings — send them there
                // instead of silently snapping the toggle back (audit §4).
                SMAppService.openSystemSettingsLoginItems()
            }
        }
    }

    /// System Settings can change this behind our back — re-read whenever a
    /// menu or settings pane appears (audit §4: stale state).
    func refreshLaunchAtLogin() {
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    /// Last N lines of the engine log, for the main window's live tail.
    nonisolated func logTail(lines: Int) -> String {
        guard let data = try? Data(contentsOf: FileManager.default
            .homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/joat/brain.log")),
            let text = String(data: data.suffix(64_000), encoding: .utf8)
        else { return "" }
        return text.split(separator: "\n", omittingEmptySubsequences: false)
            .suffix(lines).joined(separator: "\n")
    }
}
