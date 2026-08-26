import AppKit
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
    enum State: Equatable { case stopped, running, crashed(Int32) }

    @Published var state: State = .stopped
    @Published var launchAtLogin: Bool = SMAppService.mainApp.status == .enabled

    private var process: Process?
    private var userStopped = false
    private var recentCrashes: [Date] = []

    private let defaults = UserDefaults.standard
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/joat/brain.log")

    var statusLine: String {
        switch state {
        case .running: "JOAT · engine running"
        case .stopped: "JOAT · stopped"
        case .crashed(let code): "JOAT · engine crashed (\(code))"
        }
    }

    init() {
        start()
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
            state = .crashed(-1)
            chooseRepo()
            return
        }
        userStopped = false

        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try? FileHandle(forWritingTo: logURL)
        try? log?.seekToEnd()

        let p = Process()
        p.executableURL = repo.appendingPathComponent(".venv/bin/python")
        p.arguments = ["-u", "-m", "jarvis"]
        p.currentDirectoryURL = repo
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
            state = .crashed(-1)
        }
    }

    func stop() {
        userStopped = true
        process?.terminate()
        process = nil
        state = .stopped
    }

    func restart() {
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.start() }
    }

    private func engineExited(code: Int32) {
        process = nil
        if userStopped {
            state = .stopped
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
        }
    }
}
