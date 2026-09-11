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

    /// Extract the two navigation settings the Python engine accepts from the
    /// repository-local developer configuration.  A Finder-launched app does
    /// not inherit Terminal or launchctl variables, so the child receives an
    /// intentionally tiny allowlist rather than the whole `.env` file.
    ///
    /// A configured Mapbox token is an explicit opt-in to the local
    /// experimental navigation lane. An inherited value, including an
    /// explicit `false` flag, always wins over the repository configuration.
    nonisolated static func navigationLaunchEnvironment(
        inherited: [String: String],
        repo: URL
    ) -> [String: String] {
        var environment = inherited
        let configured = navigationConfiguration(at: repo.appendingPathComponent(".env"))

        let inheritedToken = nonEmpty(environment["MAPBOX_ACCESS_TOKEN"])
            ?? nonEmpty(environment["MAPBOX_API_KEY"])
        let configuredToken = nonEmpty(configured["MAPBOX_ACCESS_TOKEN"])
            ?? nonEmpty(configured["MAPBOX_API_KEY"])
        let configuredTokenWasUsed = inheritedToken == nil && configuredToken != nil
        if let token = inheritedToken ?? configuredToken {
            environment["MAPBOX_ACCESS_TOKEN"] = token
        }

        if nonEmpty(environment["JOAT_EXPERIMENTAL_NAVIGATION"]) == nil {
            if let enabled = nonEmpty(configured["JOAT_EXPERIMENTAL_NAVIGATION"]) {
                environment["JOAT_EXPERIMENTAL_NAVIGATION"] = enabled
            } else if configuredTokenWasUsed {
                environment["JOAT_EXPERIMENTAL_NAVIGATION"] = "1"
            }
        }

        // The engine contract has one token name.  Do not pass the legacy
        // name through or accidentally expose unrelated `.env` values.
        environment.removeValue(forKey: "MAPBOX_API_KEY")
        return environment
    }

    private nonisolated static func navigationConfiguration(at url: URL) -> [String: String] {
        guard let contents = try? String(contentsOf: url, encoding: .utf8) else {
            return [:]
        }
        var configuration: [String: String] = [:]
        for rawLine in contents.split(whereSeparator: \.isNewline) {
            var line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !line.isEmpty, !line.hasPrefix("#") else { continue }
            if line.hasPrefix("export ") {
                line.removeFirst("export ".count)
            }
            guard let separator = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<separator]).trimmingCharacters(in: .whitespaces)
            guard ["MAPBOX_ACCESS_TOKEN", "MAPBOX_API_KEY", "JOAT_EXPERIMENTAL_NAVIGATION"].contains(key) else {
                continue
            }
            var value = String(line[line.index(after: separator)...])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if value.count >= 2,
               let first = value.first,
               (first == "\"" || first == "'"),
               value.last == first {
                value.removeFirst()
                value.removeLast()
            }
            configuration[key] = value
        }
        return configuration
    }

    private nonisolated static func nonEmpty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty else { return nil }
        return trimmed
    }

    /// Hosted XCTest launches the application target to inject its bundle.
    /// Starting the real notification watchers and Python engine from that
    /// short-lived host would request Files & Folders access for a disposable
    /// Debug identity and make its prompt look like the installed JOAT app.
    nonisolated static func shouldRunAppServices(
        environment: [String: String],
        previewBundle: Bool = false
    ) -> Bool {
        !previewBundle && environment["XCTestConfigurationFilePath"] == nil && environment["JOAT_SHELL_PREVIEW"] != "1"
    }

    nonisolated static var isDesignPreviewBundle: Bool {
        let value = Bundle.main.object(forInfoDictionaryKey: "JOATDesignPreview")
        return (value as? Bool == true) || (value as? String)?.uppercased() == "YES"
    }

    let isPreview = isDesignPreviewBundle || ProcessInfo.processInfo.environment["JOAT_SHELL_PREVIEW"] == "1"

    static let enginePort =
        (UserDefaults.standard.object(forKey: "EnginePort") as? Int)
        ?? defaultEnginePort(bundleIdentifier: Bundle.main.bundleIdentifier)

    static let sttPort =
        (UserDefaults.standard.object(forKey: "SttPort") as? Int)
        ?? defaultSttPort(bundleIdentifier: Bundle.main.bundleIdentifier)

    @Published var state: State = .stopped
    @Published private(set) var isTransitioning = false
    @Published private(set) var lastStartedAt: Date?
    @Published var launchAtLogin: Bool = SMAppService.mainApp.status == .enabled
    @Published var launchAtLoginNotice: String?
    @Published var accessibilityTrusted = false
    @Published var pairingNotice: String?
    @Published var calendarAccess = "not determined"
    @Published var remindersAccess = "not determined"
    @Published var g2DiagnosticsEnabled: Bool
    @Published var g2DiagnosticNotice: String?
    @Published var privateTailnetGateway: Bool

    private var process: Process?
    private var userStopped = false
    private var recentCrashes: [Date] = []
    private var pendingStopCompletion: (() -> Void)?
    private var scheduledRecovery: Task<Void, Never>?

    private let defaults = UserDefaults.standard
    private let g2DiagnosticsKey = "G2DiagnosticsEnabled"
    private let privateTailnetGatewayKey = "PrivateTailnetGateway"
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
        g2DiagnosticsEnabled = UserDefaults.standard.bool(forKey: "G2DiagnosticsEnabled")
        privateTailnetGateway = UserDefaults.standard.bool(forKey: "PrivateTailnetGateway")
        guard Self.shouldRunAppServices(
            environment: ProcessInfo.processInfo.environment,
            previewBundle: Self.isDesignPreviewBundle
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
        guard !isPreview else { return }
        NotificationWatcher.shared.requestAccess()
    }

    func refreshPersonalDataAccess() {
        guard !isPreview else { return }
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
        guard !isPreview else { return }
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
        guard !isPreview else { return }
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

    /// The app, rather than an inherited shell environment, owns the one-time
    /// home-brain network mode. Login Items therefore retain it after a
    /// restart, even when the Mini has no interactive terminal open.
    nonisolated static func engineArguments(
        enginePort: Int,
        sttPort: Int,
        g2DiagnosticsEnabled: Bool,
        privateTailnetGateway: Bool
    ) -> [String] {
        var arguments = [
            "-u", "-m", "joat",
            "--port", String(enginePort),
            "--stt-port", String(sttPort),
        ]
        if g2DiagnosticsEnabled { arguments.append("--g2-diagnostics") }
        if privateTailnetGateway { arguments.append("--private-tailnet-gateway") }
        return arguments
    }

    func start() {
        guard !isPreview, process == nil, !isTransitioning else { return }
        scheduledRecovery?.cancel()
        scheduledRecovery = nil
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
        p.arguments = Self.engineArguments(
            enginePort: Self.enginePort,
            sttPort: Self.sttPort,
            g2DiagnosticsEnabled: g2DiagnosticsEnabled,
            privateTailnetGateway: privateTailnetGateway
        )
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
        env = Self.navigationLaunchEnvironment(inherited: env, repo: repo)
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
            lastStartedAt = Date()
            state = .running
        } catch {
            fail("engine spawn failed: \(error.localizedDescription)")
        }
    }

    func stop(completion: (() -> Void)? = nil) {
        scheduledRecovery?.cancel()
        scheduledRecovery = nil
        userStopped = true
        // A later Stop (including Quit) supersedes a pending Restart.
        pendingStopCompletion = completion
        guard let p = process else {
            isTransitioning = false
            state = .stopped
            pendingStopCompletion = nil
            completion?()
            return
        }
        guard !isTransitioning else { return }
        isTransitioning = true
        if p.isRunning { p.terminate() }
        // Escalate if the engine ignores SIGTERM (audit §C2).
        let pid = p.processIdentifier
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            // Never let an old stop timeout kill a replacement process.
            if self?.process === p, p.isRunning { kill(pid, SIGKILL) }
        }
    }

    func restart() {
        guard !isTransitioning else { return }
        stop { [weak self] in self?.start() }
    }

    private func engineExited(code: Int32) {
        NativeBridgeServer.shared.authorizePeerProcessID(nil)
        process = nil
        isTransitioning = false
        if userStopped {
            state = .stopped
            let completion = pendingStopCompletion
            pendingStopCompletion = nil
            completion?()
            return
        }
        state = .crashed(code)
        // Supervision with a circuit breaker: restart unless it's crash-looping.
        recentCrashes.append(Date())
        recentCrashes.removeAll { $0 < Date().addingTimeInterval(-300) }
        if recentCrashes.count <= 5 {
            scheduledRecovery = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(3)) }
                catch { return }
                guard let self, !self.userStopped else { return }
                self.start()
            }
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
        guard !isPreview else { return }
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
        guard !isPreview else { return }
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
            ? (state == .running
               ? "G2 diagnostic trace is selected. Restarting the engine to begin this capture."
               : "G2 diagnostic trace is selected. Start the engine to begin this capture.")
            : "G2 diagnostic trace is off. The redacted capture remains available to export."
        appendLog(enabled ? "G2 diagnostic trace enabled" : "G2 diagnostic trace disabled")
        if state == .running { restart() }
    }

    /// Select the supported unattended-Mini listener topology. This does not
    /// restart a live wearer session: the choice is applied by the next normal
    /// engine start, when the Mini's Tailscale Serve route is ready.
    func setPrivateTailnetGateway(_ enabled: Bool) {
        guard !isPreview else { return }
        guard enabled != privateTailnetGateway else { return }
        defaults.set(enabled, forKey: privateTailnetGatewayKey)
        privateTailnetGateway = enabled
        appendLog(
            enabled
                ? "private Tailnet home-brain gateway selected; restart to apply"
                : "direct development gateway selected; restart to apply"
        )
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
        guard !isPreview else { return }
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            launchAtLogin = SMAppService.mainApp.status == .enabled
            launchAtLoginNotice = SMAppService.mainApp.status == .requiresApproval
                ? "Approve JOAT in System Settings → General → Login Items to finish enabling launch at login."
                : nil
            if SMAppService.mainApp.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
        } catch {
            launchAtLogin = SMAppService.mainApp.status == .enabled
            launchAtLoginNotice = "Could not change launch at login: \(error.localizedDescription)"
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
