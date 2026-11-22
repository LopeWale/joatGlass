import SwiftUI
import Contacts
import UniformTypeIdentifiers
import UserNotifications

@main
struct JOATBrainApp: App {
    init() {
        // Started at process launch, not when the screen appears: an AppIntent
        // launch can start JOAT Brain in the background with no screen.
        JOATRuntime.shared.start(reason: "launch")
    }

    var body: some Scene {
        WindowGroup { ContentView() }
    }
}

/// Everything the brain runs, created once per process whether the app was
/// opened by hand or launched in the background by `StartBrainIntent`.
@MainActor
final class JOATRuntime: NSObject, UNUserNotificationCenterDelegate {
    static let shared = JOATRuntime()

    /// The plugin dials 8787. JOAT_LOOPBACK_PORT is for the Simulator only:
    /// it shares the Mac's 127.0.0.1, where the Mac's JOAT holds 8787.
    let server = LoopbackServer(port: UInt16(ProcessInfo.processInfo.environment["JOAT_LOOPBACK_PORT"] ?? "") ?? 8787)
    let keepAlive = KeepAlive()
    /// Answers the wearer: Apple dictation, taught the assistant's name.
    let stt = Transcriber(kind: .dictation(words: ["JOAT", "Hey JOAT"]), label: "dictation")
    let voice = Voice()
    /// Prep notes for QuickNotes' AI cues (Prep.swift, Cues.swift).
    let prep = Prep(claude: ClaudeLink())
    /// The saved notes, for the Notes tab.
    let notes = NotesModel()
    /// The Mac's Agent Terminal controls over Tailscale, for the Agents tab.
    let macTerminal = MacTerminal()
    /// Whether Claude is configured on this phone.
    var claudeConfigured: Bool { OnPhoneAI.shared.claudeConfigured }
    let endurance: Endurance
    private(set) var brain: Brain?
    var running: Bool { brain != nil }

    private override init() {
        endurance = Endurance(server: server)
        super.init()
    }

    func start(reason: String) {
        guard brain == nil else {
            // Already running: ensure background audio and loopback listener are healthy.
            keepAlive.check()
            server.ensureListening(reason: reason, rebuildIfIdle: true)
            return
        }
        Log.add("app launched (\(reason), \(UIApplication.shared.applicationState == .background ? "background" : "foreground"))")
        UNUserNotificationCenter.current().delegate = self
        server.start()
        keepAlive.start()
        let b = Brain(server: server, stt: stt, shadow: nil, voice: voice)
        brain = b
        let cues = Cues(claude: ClaudeLink())
        cues.prep = prep
        b.quickNotes.prep = prep
        b.quickNotes.cues = cues
        b.quickNotes.onStoreChange = { [notes] in Task { @MainActor in notes.reload() } }
        b.start()
        // Any legacy notebook draft left open when JOAT Brain last stopped is saved.
        Notebook.recoverDraft(quickNotes: b.quickNotes)
        endurance.start()
        #if DEBUG
        DebugSeed.launched(b)
        #endif
    }

    /// Prompts once when opened for Contacts and Reminders access if undetermined.
    func askPermissions() {
        #if DEBUG
        if ProcessInfo.processInfo.environment["JOAT_SKIP_SIM_PROMPTS"] == "1" { return }
        #endif
        if CNContactStore.authorizationStatus(for: .contacts) == .notDetermined {
            CNContactStore().requestAccess(for: .contacts) { granted, _ in
                Log.add("contacts \(granted ? "allowed" : "not allowed")")
            }
        }
        if RemindersStore.status == .notDetermined {
            Task { await brain?.reminders.store.requestAccess() }
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list])
    }
}

/// A file shared to JOAT Brain, waiting for its prep note.
struct SharedFile: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

private struct LaunchRoute {
    let tab: String
    let notesFilter: NotesFilter
    let showDiagnostics: Bool
    let showNavigation: Bool

    static func resolve() -> LaunchRoute {
        let rawArg = Self.commandLineValue(for: "-JOATTab")
            ?? UserDefaults.standard.string(forKey: "JOATTab")
            ?? ProcessInfo.processInfo.environment["JOAT_TAB"]
            ?? "home"
        switch rawArg.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "mail":
            return LaunchRoute(tab: "mail", notesFilter: .all, showDiagnostics: false, showNavigation: false)
        case "notes":
            return LaunchRoute(tab: "notes", notesFilter: .all, showDiagnostics: false, showNavigation: false)
        case "pages", "notebook":
            return LaunchRoute(tab: "notes", notesFilter: .pages, showDiagnostics: false, showNavigation: false)
        case "voice":
            return LaunchRoute(tab: "notes", notesFilter: .voice, showDiagnostics: false, showNavigation: false)
        case "prep":
            return LaunchRoute(tab: "notes", notesFilter: .prep, showDiagnostics: false, showNavigation: false)
        case "agents", "terminal":
            return LaunchRoute(tab: "agents", notesFilter: .all, showDiagnostics: false, showNavigation: false)
        case "diagnostics":
            return LaunchRoute(tab: "home", notesFilter: .all, showDiagnostics: true, showNavigation: false)
        case "navigation", "nav", "places":
            return LaunchRoute(tab: "home", notesFilter: .all, showDiagnostics: false, showNavigation: true)
        default:
            return LaunchRoute(tab: "home", notesFilter: .all, showDiagnostics: false, showNavigation: false)
        }
    }

    private static func commandLineValue(for flag: String) -> String? {
        let args = ProcessInfo.processInfo.arguments
        guard let idx = args.firstIndex(of: flag), args.indices.contains(idx + 1) else {
            return nil
        }
        return args[idx + 1]
    }
}

struct ContentView: View {
    @StateObject private var permissions = Permissions()
    @ObservedObject private var notes = JOATRuntime.shared.notes
    @ObservedObject private var macTerminal = JOATRuntime.shared.macTerminal
    @ObservedObject private var keepAlive = JOATRuntime.shared.keepAlive
    @ObservedObject private var server = JOATRuntime.shared.server
    @Environment(\.scenePhase) private var phase

    @State private var tab: String
    @State private var notesFilter: NotesFilter
    @State private var showDiagnostics: Bool
    @State private var showNavigation: Bool
    @State private var shared: SharedFile?

    init() {
        let route = LaunchRoute.resolve()
        _tab = State(initialValue: route.tab)
        _notesFilter = State(initialValue: route.notesFilter)
        _showDiagnostics = State(initialValue: route.showDiagnostics)
        _showNavigation = State(initialValue: route.showNavigation)
    }

    var body: some View {
        tabContent
            .modifier(DebugDuoTraitModifier())
    }

    private var tabContent: some View {
        TabView(selection: $tab) {
            HomeView(
                permissions: permissions,
                selectedTab: $tab,
                showDiagnostics: $showDiagnostics,
                showNavigation: $showNavigation
            )
            .tabItem { Label("Home", systemImage: "eyeglasses") }
            .tag("home")

            MailView()
                .tabItem { Label("Mail", systemImage: "envelope.fill") }
                .tag("mail")

            NotesView(model: notes, filter: $notesFilter)
                .tabItem { Label("Notes", systemImage: "pencil.and.scribble") }
                .tag("notes")

            AgentsView(model: macTerminal)
                .tabItem { Label("Agents", systemImage: "terminal") }
                .tag("agents")
        }
        .tint(JoatTheme.accent)
        // Share › JOAT Brain on a PDF or text file (CFBundleDocumentTypes):
        // choose its prep folder and jump straight to Notes › Prep.
        .onOpenURL { url in shared = SharedFile(url: url) }
        .sheet(item: $shared) { file in
            PrepTargetPicker(what: "“\(file.url.lastPathComponent)”", cancelled: { Prep.dropInboxCopy(file.url) }) { target in
                JOATRuntime.shared.prep.addFile(file.url, to: target)
                notesFilter = .prep
                tab = "notes"
            }
        }
        .onAppear {
            JOATRuntime.shared.start(reason: "opened")
            JOATRuntime.shared.askPermissions()
            notes.reload()
        }
        .onChange(of: phase) { _, new in
            Log.add("scene \(new)")
            if new == .active {
                keepAlive.check()
                server.ensureListening(reason: "app opened", rebuildIfIdle: true)
                notes.reload()
                Task { await permissions.refresh() }
                if tab == "agents" || macTerminal.state.login?.isActive == true {
                    Task { await macTerminal.fetchState(quiet: true) }
                }
            }
        }
    }
}

private struct DebugDuoTraitModifier: ViewModifier {
    func body(content: Content) -> some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["JOAT_DUO_UNFOLDED"] == "1" {
            content
                .environment(\.horizontalSizeClass, .regular)
                .onAppear {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                        for scene in UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }) {
                            scene.requestGeometryUpdate(.iOS(interfaceOrientations: .landscapeRight)) { _ in }
                        }
                    }
                }
        } else {
            content
                .onAppear {
                    if ProcessInfo.processInfo.environment["JOAT_FORCE_PORTRAIT"] == "1" {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                            for scene in UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }) {
                                scene.requestGeometryUpdate(.iOS(interfaceOrientations: .portrait)) { _ in }
                            }
                        }
                    }
                }
        }
        #else
        content
        #endif
    }
}
