import AppKit
import ApplicationServices
import ServiceManagement
import SwiftUI

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

    /// Single source of truth for the engine's WS port — passed to the
    /// engine via --port and used by the stale-engine reaper. Overridable
    /// without a rebuild: `defaults write com.joat.shell EnginePort 9090`
    /// (UserDefaults, not an env var — GUI apps launched from Finder or as
    /// login items don't inherit the shell environment). NOTE: the hub
    /// plugin's VITE_MAC_WS_URL and the packed app.json whitelist must
    /// still agree with whatever this resolves to.
    static let enginePort =
        (UserDefaults.standard.object(forKey: "EnginePort") as? Int) ?? 8787

    @Published var state: State = .stopped
    @Published var launchAtLogin: Bool = SMAppService.mainApp.status == .enabled
    @Published var accessibilityTrusted: Bool = AXIsProcessTrusted()

    private var process: Process?
    private var userStopped = false
    private var recentCrashes: [Date] = []
    private var pendingStopCompletion: (() -> Void)?

    private let defaults = UserDefaults.standard
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/joat/brain.log")

    var statusLine: String {
        switch state {
        case .running: "JOAT · engine running"
        case .stopped: "JOAT · stopped"
        case .crashed(let code): "JOAT · engine crashed (\(code))"
        case .failed(let why): "JOAT · \(why)"
        }
    }

    init() {
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
        if let h = try? FileHandle(forWritingTo: logURL) {
            try? h.seekToEnd()
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
        try? log?.seekToEnd()

        let p = Process()
        p.executableURL = repo.appendingPathComponent(".venv/bin/python")
        p.arguments = ["-u", "-m", "joat", "--port", String(Self.enginePort)]
        p.currentDirectoryURL = repo
        // GUI apps do not inherit the login shell's PATH; without brew and
        // ~/.local the engine silently degrades (no whisperkit-cli, no codex).
        var env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:"
            + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in self?.engineExited(code: proc.terminationStatus) }
        }
        do {
            try p.run()
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
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.start() }
    }

    private func engineExited(code: Int32) {
        process = nil
        if userStopped {
            state = .stopped
            pendingStopCompletion?()
            pendingStopCompletion = nil
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

    /// Exactly one engine may exist. An app relaunch (or a crashed shell)
    /// can leave an orphan holding the engine port — the new engine would
    /// then crash-loop on bind. Reap by PORT OWNERSHIP: the kernel reports
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
            + "[ -n \"$PIDS\" ] && kill $PIDS && sleep 1; "
            + "pkill -f -- '[-]u -m joat' && sleep 1; true"]
        try? reap.run()
        reap.waitUntilExit()
    }

    // MARK: conveniences

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
