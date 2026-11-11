import SwiftUI
import Contacts
import UniformTypeIdentifiers
import UserNotifications

@main
struct JOATBrainApp: App {
    init() {
        // Started at process launch, not when the screen appears: a Shortcuts
        // automation launches JOAT Brain in the background with no screen.
        JOATRuntime.shared.start(reason: "launch")
    }

    var body: some Scene {
        WindowGroup { ContentView() }
    }
}

/// Everything the brain runs, created once per process whether the app was
/// opened by hand or launched in the background by a Shortcuts action
/// (Intents.swift).
@MainActor
final class JOATRuntime: NSObject, UNUserNotificationCenterDelegate {
    static let shared = JOATRuntime()

    /// The plugin dials 8787. JOAT_LOOPBACK_PORT is for the Simulator only:
    /// it shares the Mac's 127.0.0.1, where the Mac's JOAT holds 8787.
    let server = LoopbackServer(port: UInt16(ProcessInfo.processInfo.environment["JOAT_LOOPBACK_PORT"] ?? "") ?? 8787)
    let keepAlive = KeepAlive()
    /// Answers the wearer: Apple dictation, taught the assistant's name.
    // The "Compare engines" shadow is gone: with it on (2026-10-23, 12:13),
    // the dictation engine that answers the wearer wrote nothing at all for
    // five minutes while the shadow wrote "What, Mr.?" and "Um." on the same
    // audio. One engine, the one taught the name.
    let stt = Transcriber(kind: .dictation(words: ["JOAT", "Hey JOAT"]), label: "dictation")
    let voice = Voice()
    /// Prep notes for QuickNotes' AI cues (Prep.swift, Cues.swift).
    let prep = Prep(claude: ClaudeLink())
    /// The saved notes, for the Notes tab.
    let notes = NotesModel()
    /// The build carries a Claude key (answers, summaries, cues).
    let claudeConfigured = ClaudeLink().configured
    let endurance: Endurance
    private(set) var brain: Brain?
    var running: Bool { brain != nil }

    private override init() {
        endurance = Endurance(server: server)
        super.init()
    }

    func start(reason: String) {
        guard brain == nil else {
            // Already running (a Shortcuts launch, the app opened): make sure
            // what keeps it reachable is up, after a call or a suspension.
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
        // A notebook page left open when JOAT Brain last stopped is saved.
        Notebook.recoverDraft(quickNotes: b.quickNotes)
        endurance.start()
        #if DEBUG
        DebugSeed.launched(b)
        #endif
    }

    /// Asked once, with the app open: notifications carry confirmed texts to
    /// the Shortcuts automation; Contacts turns a name into a number.
    func askPermissions() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            Log.add("notifications \(granted ? "allowed" : "not allowed")")
        }
        if CNContactStore.authorizationStatus(for: .contacts) == .notDetermined {
            CNContactStore().requestAccess(for: .contacts) { granted, _ in
                Log.add("contacts \(granted ? "allowed" : "not allowed")")
            }
        }
        // A first start launched in the background could not show this prompt.
        if RemindersStore.status == .notDetermined {
            Task { await brain?.reminders.store.requestAccess() }
        }
    }

    /// JOAT's notification still reaches the automation with the app open.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }
}

/// A file shared to JOAT Brain, waiting for its prep note.
struct SharedFile: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

struct ContentView: View {
    @StateObject private var permissions = Permissions()
    @ObservedObject private var notes = JOATRuntime.shared.notes
    @ObservedObject private var keepAlive = JOATRuntime.shared.keepAlive
    @ObservedObject private var server = JOATRuntime.shared.server
    @Environment(\.scenePhase) private var phase
    /// `-JOATTab notes` at launch opens that tab (Simulator screenshots).
    @State private var tab = UserDefaults.standard.string(forKey: "JOATTab") ?? "home"
    @State private var shared: SharedFile?

    var body: some View {
        TabView(selection: $tab) {
            HomeView(permissions: permissions)
                .tabItem { Label("Home", systemImage: "eyeglasses") }.tag("home")
            NotesView(model: notes)
                .tabItem { Label("Notes", systemImage: "note.text") }.tag("notes")
            PrepView()
                .tabItem { Label("Prep", systemImage: "doc.text.magnifyingglass") }.tag("prep")
            MessagesView()
                .tabItem { Label("Messages", systemImage: "message") }.tag("messages")
            DiagnosticsView()
                .tabItem { Label("Diagnostics", systemImage: "stethoscope") }.tag("diagnostics")
        }
        // Share › JOAT Brain on a PDF or text file (CFBundleDocumentTypes):
        // which prep note it goes into (the wearer's choice, 2026-11-11).
        .onOpenURL { url in shared = SharedFile(url: url) }
        .sheet(item: $shared) { file in
            PrepTargetPicker(what: "“\(file.url.lastPathComponent)”", cancelled: { Prep.dropInboxCopy(file.url) }) { target in
                JOATRuntime.shared.prep.addFile(file.url, to: target)
                tab = "prep"
            }
        }
        .onAppear {
            JOATRuntime.shared.start(reason: "opened")
            JOATRuntime.shared.askPermissions()
            notes.reload()
        }
        .onChange(of: phase) { _, new in
            Log.add("scene \(new)")
            // Back in front after the app may have been suspended: a dead
            // keep-alive or a reclaimed listening socket is rebuilt here.
            if new == .active {
                keepAlive.check()
                server.ensureListening(reason: "app opened", rebuildIfIdle: true)
                notes.reload()
                Task { await permissions.refresh() }
            }
        }
    }
}
