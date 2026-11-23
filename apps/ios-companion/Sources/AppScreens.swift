import SwiftUI
import UIKit
import Speech
import EventKit
import Contacts
import CoreLocation
import UniformTypeIdentifiers
import PencilKit

// JOAT Brain's 4-tab companion interface for iPhone & iPhone Duo:
//   1. Home   — Even G2 hero status, 2×2 subsystem tiles, AI & voice settings, Diagnostics
//   2. Mail   — Standalone multi-account Gmail (MailView.swift)
//   3. Notes  — Apple Pencil Notebook Pages, Voice QuickNotes & Prep folders (Duo SplitView)
//   4. Agents — Tailscale Mac Agent Terminal controls

extension QuickNotes.VoiceNote: Identifiable {}
extension QuickNotes.TextNote: Identifiable {}

/// Segmented filter in the `Notes` tab (`All | Pages | Voice | Prep`).
enum NotesFilter: String, CaseIterable, Identifiable {
    case all = "All"
    case pages = "Pages"
    case voice = "Voice"
    case prep = "Prep"

    var id: String { rawValue }
}

/// The saved notes, for the Notes tab; QuickNotes tells it when they change.
@MainActor
final class NotesModel: ObservableObject {
    @Published private(set) var voice: [QuickNotes.VoiceNote] = []
    @Published private(set) var text: [QuickNotes.TextNote] = []

    func reload() {
        guard let q = JOATRuntime.shared.brain?.quickNotes else { return }
        voice = q.savedNotes
        text = q.textNotes
    }
}

/// What JOAT Brain needs from iOS, and whether it has it (Speech, Location, Reminders, Calendar, Contacts).
@MainActor
final class Permissions: ObservableObject {
    enum State { case allowed, notAsked, denied }
    struct Item: Identifiable {
        let id: String
        let name: String
        let why: String
        let state: State
    }
    @Published private(set) var items: [Item] = []

    init() {
        AssistantRuntimeHooks.sharedVoice = { JOATRuntime.shared.voice }
        AssistantRuntimeHooks.sharedPrep = { JOATRuntime.shared.prep }
        AssistantRuntimeHooks.sharedMacTerminal = { JOATRuntime.shared.macTerminal }
    }

    func refresh() async {
        let speech: State = switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        let locationState: State = switch PhoneLocation.shared.authorization {
        case .authorizedWhenInUse, .authorizedAlways: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        let reminders: State = switch EKEventStore.authorizationStatus(for: .reminder) {
        case .fullAccess: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        let calendar: State = switch EKEventStore.authorizationStatus(for: .event) {
        case .fullAccess: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        let contacts: State = switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized, .limited: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        items = [
            Item(id: "speech", name: "Speech recognition", why: "Hears what you say to the glasses.", state: speech),
            Item(id: "location", name: "Location", why: "Streams GPS and street maps to your glasses during navigation.", state: locationState),
            Item(id: "reminders", name: "Reminders", why: "Adds, lists and completes your reminders.", state: reminders),
            Item(id: "calendar", name: "Calendar", why: "Reads your calendar events when you ask about your schedule.", state: calendar),
            Item(id: "contacts", name: "Contacts", why: "Resolves people you text or email by voice.", state: contacts),
        ]
    }

    /// Ask iOS; a permission already refused can only change in Settings.
    func fix(_ item: Item) async {
        if item.state == .denied {
            if let url = URL(string: UIApplication.openSettingsURLString) { await UIApplication.shared.open(url) }
            return
        }
        switch item.id {
        case "speech":
            _ = await withCheckedContinuation { c in SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) } }
            JOATRuntime.shared.stt.start()
        case "location":
            _ = await PhoneLocation.shared.requestPermission()
        case "reminders":
            await JOATRuntime.shared.brain?.reminders.store.requestAccess()
        case "calendar":
            if let store = JOATRuntime.shared.brain?.reminders.store.store {
                _ = try? await store.requestFullAccessToEvents()
                if EKEventStore.authorizationStatus(for: .event) == .fullAccess { store.reset() }
            } else {
                _ = try? await EKEventStore().requestFullAccessToEvents()
            }
        case "contacts":
            _ = try? await CNContactStore().requestAccess(for: .contacts)
        default:
            break
        }
        await refresh()
    }
}

// MARK: - Home

struct HomeView: View {
    @ObservedObject private var server = JOATRuntime.shared.server
    @ObservedObject private var keepAlive = JOATRuntime.shared.keepAlive
    @ObservedObject private var stt = JOATRuntime.shared.stt
    @ObservedObject private var voice = JOATRuntime.shared.voice
    @ObservedObject private var onPhoneAI = OnPhoneAI.shared
    @ObservedObject private var gmailStore = GmailAccountStore.shared
    @ObservedObject private var macTerminal = JOATRuntime.shared.macTerminal
    @ObservedObject var permissions: Permissions

    @Binding var selectedTab: String
    @Binding var showDiagnostics: Bool
    @Binding var showNavigation: Bool

    @AppStorage(Brain.followUpKey) private var followUpSeconds: Double = 8
    @AppStorage(Cues.enabledKey) private var cuesOn = true

    @State private var keySheetTarget: OnPhoneKeyKind?
    @State private var showingQuickAISheet = false

    init(
        permissions: Permissions,
        selectedTab: Binding<String> = .constant("home"),
        showDiagnostics: Binding<Bool> = .constant(false),
        showNavigation: Binding<Bool> = .constant(false)
    ) {
        self.permissions = permissions
        self._selectedTab = selectedTab
        self._showDiagnostics = showDiagnostics
        self._showNavigation = showNavigation
    }

    private var glassesOK: Bool { server.authenticated && server.clients > 0 }
    private var listening: Bool { stt.engine.hasSuffix("listening") }
    private var readyAgentsCount: Int {
        macTerminal.state.agents.filter { $0.installed && $0.loggedIn }.count
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    heroCard

                    subsystemGrid

                    if let navigation = JOATRuntime.shared.brain?.navigation {
                        HomeNavigationCard(
                            navigation: navigation,
                            openNavigation: { showNavigation = true }
                        )
                    }

                    if !glassesOK {
                        pairingGuideCard
                    }

                    let missing = permissions.items.filter { $0.state != .allowed }
                    if !missing.isEmpty {
                        permissionsBanner(missing)
                    }

                    aiAndVoiceSettingsCard
                }
                .padding(16)
                .frame(maxWidth: 860)
                .frame(maxWidth: .infinity)
            }
            .background(JoatTheme.surface.ignoresSafeArea())
            .navigationTitle("JOAT Brain")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        showDiagnostics = true
                    } label: {
                        Label("Diagnostics", systemImage: "stethoscope")
                    }
                }
            }
            .navigationDestination(isPresented: $showDiagnostics) {
                DiagnosticsView(embedded: true)
            }
            .navigationDestination(isPresented: $showNavigation) {
                if let navigation = JOATRuntime.shared.brain?.navigation {
                    NavigationPlacesView(navigation: navigation)
                } else {
                    ContentUnavailableView(
                        "Starting Navigation…",
                        systemImage: "location.north.line.fill",
                        description: Text("Initializing Apple Maps navigation engine on this iPhone.")
                    )
                    .onAppear {
                        JOATRuntime.shared.start(reason: "navigation")
                    }
                }
            }
            .sheet(item: $keySheetTarget) { kind in
                OnPhoneAPIKeySheet(kind: kind, onPhoneAI: onPhoneAI)
            }
            .sheet(isPresented: $showingQuickAISheet) {
                OnPhoneAISettingsSheet(onPhoneAI: onPhoneAI)
            }
            .task {
                await permissions.refresh()
                if macTerminal.configured && macTerminal.state.agents.isEmpty {
                    await macTerminal.fetchState(quiet: true)
                }
            }
            .refreshable {
                await permissions.refresh()
                if macTerminal.configured {
                    await macTerminal.fetchState(quiet: true)
                }
            }
        }
    }

    private var heroCard: some View {
        JoatHeroCard(
            eyebrow: "Even Realities G2",
            title: glassesOK ? "Glasses connected" : "Waiting for glasses",
            subtitle: glassesOK
                ? "JOAT on the glasses is linked directly to this iPhone (\(server.pongs) heartbeats)."
                : "Open the Even Realities app and launch JOAT to connect your glasses.",
            icon: "eyeglasses",
            statusColor: glassesOK ? JoatTheme.accent : JoatTheme.amber,
            actionTitle: keepAlive.active ? nil : "Restart",
            action: keepAlive.active ? nil : { keepAlive.start() }
        ) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    JoatStatusPill(
                        label: keepAlive.active ? "Keep-Alive Active" : "Keep-Alive Paused",
                        color: keepAlive.active ? JoatTheme.accent : JoatTheme.amber,
                        icon: keepAlive.active ? "waveform.badge.checkmark" : "pause.circle.fill"
                    )
                    JoatStatusPill(
                        label: listening ? "On-Device Dictation" : "Mic Standby",
                        color: listening ? JoatTheme.accent : JoatTheme.amber,
                        icon: listening ? "mic.fill" : "mic.slash"
                    )
                }
            }
        }
    }

    private var subsystemGrid: some View {
        let enabledMailCount = gmailStore.enabledAccounts.count
        let agentSummary: String = {
            guard macTerminal.configured else { return "Not linked" }
            return "\(readyAgentsCount) \(readyAgentsCount == 1 ? "agent" : "agents") ready"
        }()

        return LazyVGrid(
            columns: [
                GridItem(.flexible(), spacing: 12),
                GridItem(.flexible(), spacing: 12),
            ],
            spacing: 12
        ) {
            JoatMetricTile(
                title: "On-Phone AI",
                value: onPhoneAI.activeProviderSummary,
                detail: onPhoneAI.anyConfigured ? "Streaming SSE ready" : "Tap to add API key",
                icon: "sparkles",
                tint: onPhoneAI.anyConfigured ? JoatTheme.accent : JoatTheme.amber
            ) {
                showingQuickAISheet = true
            }

            JoatMetricTile(
                title: "Gmail",
                value: "\(enabledMailCount) connected",
                detail: gmailStore.defaultAccount.map { "Default: \($0.label)" } ?? "Tap to connect Gmail",
                icon: "envelope.fill",
                tint: enabledMailCount > 0 ? JoatTheme.accent : .secondary
            ) {
                selectedTab = "mail"
            }

            if let macMessages = JOATRuntime.shared.brain?.macMessages {
                MacMessagesMetricTile(macMessages: macMessages)
            } else {
                JoatMetricTile(
                    title: "Mac Messages",
                    value: "Not linked",
                    detail: "Always on Mac · Pair via build_ios.sh",
                    icon: "message.fill",
                    tint: .secondary
                )
            }

            JoatMetricTile(
                title: "Agent Terminal",
                value: agentSummary,
                detail: macTerminal.hostLabel.map { "Pinned to \($0)" }
                    ?? (macTerminal.configured ? "Pinned to Mac" : "Claude · Codex · Antigravity"),
                icon: "terminal.fill",
                tint: readyAgentsCount > 0 ? JoatTheme.accent : (macTerminal.configured ? JoatTheme.amber : .secondary)
            ) {
                selectedTab = "agents"
            }
        }
    }

    private var pairingGuideCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Connect Even G2", systemImage: "link.badge.plus")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(JoatTheme.accent)

            Step(n: 1, "Open the Even Realities app with your G2 glasses awake.")
            Step(n: 2, "Start JOAT from the glasses menu or Even Hub › JOAT.")
            Step(n: 3, "Select “This iPhone · JOAT Brain” to link directly on loopback.")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .joatCardContainer(cornerRadius: 16, padding: 14)
    }

    private func permissionsBanner(_ missing: [Permissions.Item]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "hand.raised.circle.fill")
                    .foregroundStyle(JoatTheme.amber)
                Text("Permissions Needed")
                    .font(.subheadline.weight(.semibold))
            }

            ForEach(missing) { item in
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.name)
                            .font(.subheadline.weight(.medium))
                        Text(item.why)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(item.state == .denied ? "Settings" : "Allow") {
                        Task { await permissions.fix(item) }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(JoatTheme.accent)
                    .controlSize(.small)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .joatCardContainer(cornerRadius: 16, padding: 14)
    }

    private var aiAndVoiceSettingsCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("AI & Voice Settings")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)

            VStack(spacing: 0) {
                HStack {
                    Label("Assistant Engine", systemImage: "cpu")
                        .font(.subheadline)
                    Spacer()
                    Picker("Assistant Engine", selection: $onPhoneAI.engineChoice) {
                        ForEach(AssistantEngineChoice.allCases) { choice in
                            Text(choice.title).tag(choice)
                        }
                    }
                    .labelsHidden()
                }
                .padding(.vertical, 10)

                Divider()

                Button {
                    keySheetTarget = .gemini
                } label: {
                    HStack {
                        Label(
                            onPhoneAI.geminiConfigured ? "Rotate Gemini API Key…" : "Set Gemini API Key…",
                            systemImage: "key.fill"
                        )
                        .font(.subheadline)
                        .foregroundStyle(.primary)
                        Spacer()
                        JoatStatusPill(
                            label: onPhoneAI.geminiConfigured ? "Saved" : "Not Set",
                            color: onPhoneAI.geminiConfigured ? JoatTheme.accent : .secondary,
                            icon: onPhoneAI.geminiConfigured ? "checkmark" : nil
                        )
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 10)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                Divider()

                Button {
                    keySheetTarget = .claude
                } label: {
                    HStack {
                        Label(
                            onPhoneAI.claudeConfigured ? "Rotate Claude API Key…" : "Set Claude API Key…",
                            systemImage: "key.horizontal.fill"
                        )
                        .font(.subheadline)
                        .foregroundStyle(.primary)
                        Spacer()
                        JoatStatusPill(
                            label: onPhoneAI.claudeConfigured ? "Saved" : "Not Set",
                            color: onPhoneAI.claudeConfigured ? JoatTheme.accent : .secondary,
                            icon: onPhoneAI.claudeConfigured ? "checkmark" : nil
                        )
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 10)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                Divider()

                Toggle(isOn: $voice.spokenReplies) {
                    Label("Speak replies in headphones", systemImage: "headphones")
                        .font(.subheadline)
                }
                .padding(.vertical, 10)

                if let navigation = JOATRuntime.shared.brain?.navigation {
                    Divider()

                    HomeNavigationVoiceToggleRow(navigation: navigation)
                }

                Divider()

                Toggle(isOn: $cuesOn) {
                    Label("AI cues during QuickNotes", systemImage: "sparkles")
                        .font(.subheadline)
                }
                .padding(.vertical, 10)

                Divider()

                Stepper(value: $followUpSeconds, in: 0...30, step: 1) {
                    HStack {
                        Label("Follow-up without “Hey JOAT”", systemImage: "waveform")
                            .font(.subheadline)
                        Spacer()
                        Text(followUpSeconds == 0 ? "Off" : "\(Int(followUpSeconds)) s")
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 10)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(JoatTheme.card)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
            )
        }
    }
}

private struct HomeNavigationVoiceToggleRow: View {
    @ObservedObject var navigation: Navigation

    var body: some View {
        Toggle(isOn: $navigation.spokenDirections) {
            Label("Spoken turn-by-turn cues", systemImage: "location.fill.viewfinder")
                .font(.subheadline)
        }
        .padding(.vertical, 10)
    }
}

struct HomeNavigationCard: View {
    @ObservedObject var navigation: Navigation
    let openNavigation: () -> Void

    @AppStorage("joat.navigation.preferredMode") private var preferredMode = "walking"
    @State private var launchFeedback: String?

    var body: some View {
        if let active = navigation.activeSummary {
            activeCard(active)
        } else {
            idleCard
        }
    }

    private func activeCard(_ active: Navigation.ActiveRouteSummary) -> some View {
        let metrics = [
            NavFormatters.eta(active.etaMinutes),
            NavFormatters.distance(active.remainingM),
        ].compactMap { $0 }.joined(separator: " · ")

        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                JoatStatusPill(
                    label: NavFormatters.modeDisplayTitle(active.mode),
                    color: JoatTheme.accent,
                    icon: NavFormatters.modeIcon(active.mode)
                )
                if !metrics.isEmpty {
                    JoatStatusPill(label: metrics, color: JoatTheme.accent)
                }
                Spacer()
                Button(role: .destructive) {
                    navigation.stopFromPhone()
                } label: {
                    Label("Stop", systemImage: "stop.fill")
                        .font(.caption.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            Button(action: openNavigation) {
                HStack(alignment: .center, spacing: 12) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 11, style: .continuous)
                            .fill(JoatTheme.accent.opacity(0.16))
                            .frame(width: 40, height: 40)
                        Image(systemName: NavFormatters.maneuverIcon(for: active.maneuver, arrived: active.arrived))
                            .font(.system(size: 17, weight: .bold))
                            .foregroundStyle(JoatTheme.accent)
                    }

                    VStack(alignment: .leading, spacing: 3) {
                        Text(active.destinationLabel)
                            .font(.subheadline.weight(.bold))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                        Text(active.instruction.isEmpty ? "Active turn-by-turn guidance on Even G2" : active.instruction)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }

                    Spacer(minLength: 4)

                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(JoatTheme.heroGradient)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(JoatTheme.heroBorder, lineWidth: 1)
        )
    }

    private var idleCard: some View {
        let placesCount = navigation.places.count
        let summaryText = placesCount == 0
            ? "Apple Maps search, saved places & G2 street maps"
            : "\(placesCount) saved \(placesCount == 1 ? "place" : "places") · Walk, Cycle, or Drive"

        return VStack(alignment: .leading, spacing: 12) {
            Button(action: openNavigation) {
                HStack(alignment: .center, spacing: 12) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 11, style: .continuous)
                            .fill(JoatTheme.accent.opacity(0.14))
                            .frame(width: 38, height: 38)
                        Image(systemName: "location.north.line.fill")
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(JoatTheme.accent)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Navigation & Places")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.primary)
                        Text(summaryText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }

                    Spacer(minLength: 6)

                    HStack(spacing: 4) {
                        Text("Open Map & Places")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(JoatTheme.accent)
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(JoatTheme.accent)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if !navigation.places.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(Array(navigation.places.prefix(3))) { place in
                            Button {
                                quickLaunch(place)
                            } label: {
                                HStack(spacing: 6) {
                                    Image(systemName: NavFormatters.placeIcon(for: place.name))
                                        .font(.system(size: 11, weight: .semibold))
                                        .foregroundStyle(JoatTheme.accent)
                                    Text(place.name)
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(.primary)
                                        .lineLimit(1)
                                    Image(systemName: NavFormatters.modeIcon(preferredMode))
                                        .font(.system(size: 10, weight: .semibold))
                                        .foregroundStyle(.secondary)
                                }
                                .padding(.horizontal, 11)
                                .padding(.vertical, 7)
                                .background(
                                    Capsule()
                                        .fill(JoatTheme.surface)
                                )
                                .overlay(
                                    Capsule()
                                        .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
                                )
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }

            if let launchFeedback, !launchFeedback.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "info.circle.fill")
                        .font(.caption)
                        .foregroundStyle(JoatTheme.amber)
                    Text(launchFeedback)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Open") {
                        self.launchFeedback = nil
                        openNavigation()
                    }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(JoatTheme.accent)
                    .buttonStyle(.plain)
                }
            }
        }
        .joatCardContainer(cornerRadius: 18, padding: 14)
    }

    private func quickLaunch(_ place: SavedPlace) {
        let candidate: NavCandidate?
        if let lat = place.latitude, let lon = place.longitude, NavPoint(lat, lon).isValid {
            candidate = NavCandidate(
                choiceId: "place:\(place.id)",
                label: place.name,
                address: place.address ?? "Saved place",
                point: NavPoint(lat, lon)
            )
        } else {
            candidate = nil
        }
        if let err = navigation.startFromPhone(destination: place.name, candidate: candidate, mode: preferredMode) {
            launchFeedback = err
        } else {
            launchFeedback = nil
        }
    }
}

private struct MacMessagesMetricTile: View {
    @ObservedObject var macMessages: MacMessages

    var body: some View {
        let statusValue = macMessages.reachable
            ? "Always on Mac · Live"
            : (macMessages.configured ? "Always on Mac · Standby" : "Not linked")
        let detailText = macMessages.hostLabel.map { "Pinned to \($0)" }
            ?? (macMessages.configured ? "Pinned to Mac" : "Pair via build_ios.sh")
        let tintColor: Color = macMessages.reachable
            ? JoatTheme.accent
            : (macMessages.configured ? JoatTheme.amber : .secondary)

        JoatMetricTile(
            title: "Mac Messages",
            value: statusValue,
            detail: detailText,
            icon: "message.fill",
            tint: tintColor
        )
    }
}

private enum OnPhoneKeyKind: String, Identifiable {
    case gemini
    case claude

    var id: String { rawValue }

    var providerTitle: String {
        switch self {
        case .gemini: return "Gemini"
        case .claude: return "Claude"
        }
    }

    var placeholder: String {
        switch self {
        case .gemini: return "Paste Google AI Studio API key"
        case .claude: return "Paste Anthropic API key"
        }
    }

    var footer: String {
        switch self {
        case .gemini:
            return "Saved write-only in iOS Keychain (com.joat.brain.ai) for sub-second Gemini 3.5 Flash Lite streaming and handwriting OCR."
        case .claude:
            return "Saved write-only in iOS Keychain (com.joat.brain.ai) for direct on-phone Claude streaming, summaries, and AI cues."
        }
    }
}

private struct OnPhoneAPIKeySheet: View {
    let kind: OnPhoneKeyKind
    @ObservedObject var onPhoneAI: OnPhoneAI

    @State private var enteredKey = ""
    @State private var errorText: String?
    @Environment(\.dismiss) private var dismiss

    private var isConfigured: Bool {
        switch kind {
        case .gemini: return onPhoneAI.geminiConfigured
        case .claude: return onPhoneAI.claudeConfigured
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField(kind.placeholder, text: $enteredKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("\(kind.providerTitle) API Key")
                } footer: {
                    Text(kind.footer)
                }

                if isConfigured {
                    Section {
                        Button(role: .destructive) {
                            switch kind {
                            case .gemini: onPhoneAI.clearGeminiKey()
                            case .claude: onPhoneAI.clearClaudeKey()
                            }
                            dismiss()
                        } label: {
                            Label("Clear Keychain Override", systemImage: "trash")
                        }
                    }
                }

                if let errorText, !errorText.isEmpty {
                    Section {
                        Text(errorText)
                            .font(.footnote)
                            .foregroundStyle(JoatTheme.amber)
                    }
                }
            }
            .navigationTitle("\(kind.providerTitle) Key")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let ok: Bool
                        switch kind {
                        case .gemini: ok = onPhoneAI.setGeminiKey(enteredKey)
                        case .claude: ok = onPhoneAI.setClaudeKey(enteredKey)
                        }
                        if ok {
                            enteredKey = ""
                            dismiss()
                        } else {
                            errorText = "Could not save the API key in Keychain."
                        }
                    }
                    .fontWeight(.semibold)
                    .disabled(enteredKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .tint(JoatTheme.accent)
    }
}

private struct OnPhoneAISettingsSheet: View {
    @ObservedObject var onPhoneAI: OnPhoneAI
    @State private var keyTarget: OnPhoneKeyKind?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Engine", selection: $onPhoneAI.engineChoice) {
                        ForEach(AssistantEngineChoice.allCases) { choice in
                            Text(choice.title).tag(choice)
                        }
                    }
                    LabeledContent("Active Provider", value: onPhoneAI.activeProviderSummary)
                } header: {
                    Text("Assistant Routing")
                } footer: {
                    Text("Auto prefers an on-phone key for ~600 ms streaming replies, falling back to your Mac over Tailscale when needed.")
                }

                Section("On-Phone Keychain Keys") {
                    Button {
                        keyTarget = .gemini
                    } label: {
                        HStack {
                            Label(
                                onPhoneAI.geminiConfigured ? "Rotate Gemini API Key…" : "Set Gemini API Key…",
                                systemImage: "key.fill"
                            )
                            Spacer()
                            if onPhoneAI.geminiConfigured {
                                JoatStatusPill(label: "Saved", color: JoatTheme.accent, icon: "checkmark")
                            }
                        }
                    }

                    Button {
                        keyTarget = .claude
                    } label: {
                        HStack {
                            Label(
                                onPhoneAI.claudeConfigured ? "Rotate Claude API Key…" : "Set Claude API Key…",
                                systemImage: "key.horizontal.fill"
                            )
                            Spacer()
                            if onPhoneAI.claudeConfigured {
                                JoatStatusPill(label: "Saved", color: JoatTheme.accent, icon: "checkmark")
                            }
                        }
                    }
                }
            }
            .navigationTitle("On-Phone AI")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .sheet(item: $keyTarget) { kind in
                OnPhoneAPIKeySheet(kind: kind, onPhoneAI: onPhoneAI)
            }
        }
        .tint(JoatTheme.accent)
    }
}

struct StatusRow: View {
    let ok: Bool
    let title: String
    let detail: String
    var actionTitle: String? = nil
    var perform: () -> Void = {}

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(ok ? JoatTheme.accent : JoatTheme.amber)
                .font(.title3)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let actionTitle {
                Button(actionTitle, action: perform).buttonStyle(.bordered)
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Notes (Adaptive SplitView + Pages / Voice / Prep)

enum NotesSelection: Hashable, Identifiable {
    case page(String)
    case voice(String)
    case prep(String)

    var id: String {
        switch self {
        case .page(let id): return "page:\(id)"
        case .voice(let id): return "voice:\(id)"
        case .prep(let id): return "prep:\(id)"
        }
    }
}

private struct CompactPageTarget: Identifiable {
    let id = UUID()
    let noteId: String?
    let typed: String
}

@MainActor
private enum PageThumbnailCache {
    private static var entries: [String: UIImage] = [:]

    static func thumbnail(for note: QuickNotes.VoiceNote) -> UIImage? {
        let key = "\(note.id)|\(Int(note.updated.timeIntervalSince1970))"
        if let cached = entries[key] {
            return cached
        }
        let img = PageStore.preview(PageStore.load(note.id)) ?? PageStore.images(note.id).first
        if let img {
            if entries.count > 48 {
                entries.removeAll(keepingCapacity: true)
            }
            entries[key] = img
        }
        return img
    }
}

struct NotesView: View {
    @ObservedObject var model: NotesModel
    @ObservedObject private var prep = JOATRuntime.shared.prep
    @Binding var filter: NotesFilter
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @AppStorage(Cues.enabledKey) private var cuesOn = true

    @State private var columnVisibility: NavigationSplitViewVisibility = .all
    @State private var searchQuery = ""
    @State private var selection: NotesSelection?
    @State private var compactPath: [NotesSelection] = []
    @State private var compactPageTarget: CompactPageTarget?

    init(model: NotesModel, filter: Binding<NotesFilter> = .constant(.all)) {
        self.model = model
        self._filter = filter
    }

    private var filteredVoiceNotes: [QuickNotes.VoiceNote] {
        let base: [QuickNotes.VoiceNote]
        switch filter {
        case .all:
            base = model.voice
        case .pages:
            base = model.voice.filter { $0.page == true }
        case .voice:
            base = model.voice.filter { $0.page != true }
        case .prep:
            return []
        }
        let q = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return base }
        return base.filter { n in
            n.title.lowercased().contains(q)
                || (n.summary?.lowercased().contains(q) == true)
                || n.paragraphs.contains(where: { $0.lowercased().contains(q) })
                || n.keyPoints.contains(where: { $0.lowercased().contains(q) })
                || n.todos.contains(where: { $0.lowercased().contains(q) })
        }
    }

    private var filteredTextNotes: [QuickNotes.TextNote] {
        guard filter == .all || filter == .voice else { return [] }
        let q = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return model.text }
        return model.text.filter { $0.text.lowercased().contains(q) }
    }

    private var filteredPrepNotes: [Prep.Note] {
        let q = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return prep.notes }
        return prep.notes.filter { note in
            note.title.lowercased().contains(q)
                || note.summary.lowercased().contains(q)
                || note.points.contains(where: { $0.lowercased().contains(q) })
                || note.items.contains(where: { $0.title.lowercased().contains(q) || $0.text.lowercased().contains(q) })
        }
    }

    var body: some View {
        Group {
            if horizontalSizeClass == .regular {
                splitLayout
            } else {
                compactLayout
            }
        }
        .fullScreenCover(item: $compactPageTarget) { target in
            PageEditorView(
                noteId: target.noteId,
                typed: target.typed,
                drawing: target.noteId.map(PageStore.load) ?? PKDrawing(),
                embedded: false
            ) { savedId in
                model.reload()
                if let savedId {
                    selection = .page(savedId)
                }
            }
        }
        #if DEBUG
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugOpen)) { note in
            let requestedId = note.object as? String
            let existingTyped = requestedId.flatMap { id in model.voice.first(where: { $0.id == id })?.typed } ?? ""
            if horizontalSizeClass == .regular, let requestedId {
                selection = .page(requestedId)
            } else {
                compactPageTarget = CompactPageTarget(noteId: requestedId, typed: existingTyped)
            }
        }
        #endif
        .onAppear {
            recoverAndReload()
            syncRegularSelectionIfNeeded()
            #if DEBUG
            NotesView.debugSeedInsightsOnce()
            #endif
        }
        // Rows show "Reading handwriting…" / "Summarizing…" and the new summary as they land.
        .onReceive(NotificationCenter.default.publisher(for: QuickNotes.insightsChangedNotification)) { _ in model.reload() }
        .onReceive(NotificationCenter.default.publisher(for: Notebook.readingChangedNotification)) { _ in model.reload() }
        .onChange(of: filter) { _, _ in
            syncRegularSelectionIfNeeded()
        }
        .onChange(of: model.voice.count) { _, _ in
            syncRegularSelectionIfNeeded()
        }
        .onChange(of: prep.notes.count) { _, _ in
            syncRegularSelectionIfNeeded()
        }
        .onChange(of: horizontalSizeClass) { _, newSize in
            if newSize == .regular {
                if selection == nil, let last = compactPath.last {
                    selection = last
                }
                syncRegularSelectionIfNeeded()
            }
        }
    }

    // MARK: - Unfolded Duo Split View

    private var splitLayout: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            notesListContent(isSplit: true)
                .navigationTitle("Notes")
                .toolbar { notesToolbar(isSplit: true) }
                .searchable(
                    text: $searchQuery,
                    prompt: filter == .prep ? "Search prep folders" : "Search notes, pages & ink"
                )
                .refreshable { recoverAndReload() }
        } detail: {
            splitDetailPane
        }
    }

    @ViewBuilder
    private var splitDetailPane: some View {
        switch selection {
        case .page(let noteId):
            if let note = model.voice.first(where: { $0.id == noteId }) {
                PageEditorView(
                    noteId: noteId,
                    typed: note.typed ?? "",
                    drawing: PageStore.load(noteId),
                    embedded: true
                ) { _ in
                    model.reload()
                }
                .id(noteId)
            } else {
                emptyDetailPlaceholder
            }

        case .voice(let noteId):
            if model.voice.contains(where: { $0.id == noteId }) {
                NoteDetailView(noteId: noteId, model: model)
                    .id(noteId)
            } else {
                emptyDetailPlaceholder
            }

        case .prep(let prepId):
            if prep.notes.contains(where: { $0.id == prepId }) {
                PrepNoteView(noteId: prepId)
                    .id(prepId)
            } else {
                emptyDetailPlaceholder
            }

        case nil:
            emptyDetailPlaceholder
        }
    }

    @ViewBuilder
    private var emptyDetailPlaceholder: some View {
        if filter == .prep {
            ContentUnavailableView {
                Label("Select a Prep Folder", systemImage: "folder.badge.gearshape")
            } description: {
                Text("Choose a prep folder on the left or create one for live QuickNotes AI cues.")
            } actions: {
                Button {
                    let id = prep.create()
                    selection = .prep(id)
                } label: {
                    Label("New Prep Note", systemImage: "folder.badge.plus")
                }
                .buttonStyle(.borderedProminent)
                .tint(JoatTheme.accent)
            }
        } else {
            ContentUnavailableView {
                Label("Apple Pencil Notebook", systemImage: "pencil.and.scribble")
            } description: {
                Text("Select a page or voice note on the left, or start a fresh stationery page with Apple Pencil.")
            } actions: {
                Button {
                    createAndSelectNewPageInSplit()
                } label: {
                    Label("New Page", systemImage: "square.and.pencil")
                }
                .buttonStyle(.borderedProminent)
                .tint(JoatTheme.accent)
            }
        }
    }

    // MARK: - Compact Cover Stack

    private var compactLayout: some View {
        NavigationStack(path: $compactPath) {
            notesListContent(isSplit: false)
                .navigationTitle("Notes")
                .toolbar { notesToolbar(isSplit: false) }
                .searchable(
                    text: $searchQuery,
                    prompt: filter == .prep ? "Search prep folders" : "Search notes, pages & ink"
                )
                .refreshable { recoverAndReload() }
                .navigationDestination(for: NotesSelection.self) { item in
                    switch item {
                    case .voice(let noteId):
                        NoteDetailView(noteId: noteId, model: model)
                    case .prep(let prepId):
                        PrepNoteView(noteId: prepId)
                    case .page(let pageId):
                        NoteDetailView(noteId: pageId, model: model)
                    }
                }
        }
    }

    // MARK: - Shared List Body

    private func notesListContent(isSplit: Bool) -> some View {
        List {
            Section {
                Picker("Filter", selection: $filter) {
                    ForEach(NotesFilter.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 8, trailing: 16))
                .listRowBackground(Color.clear)
                .listRowSeparator(.hidden)
            }

            if filter == .prep {
                prepSections(isSplit: isSplit)
            } else {
                notesAndPagesSections(isSplit: isSplit)
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .background(JoatTheme.surface.ignoresSafeArea())
    }

    @ViewBuilder
    private func notesAndPagesSections(isSplit: Bool) -> some View {
        let voices = filteredVoiceNotes
        let texts = filteredTextNotes

        if voices.isEmpty && texts.isEmpty {
            Section {
                VStack(spacing: 12) {
                    Image(systemName: filter == .pages ? "pencil.and.scribble" : (filter == .voice ? "waveform" : "note.text"))
                        .font(.system(size: 30))
                        .foregroundStyle(JoatTheme.accent)
                    Text(emptyNotesTitle)
                        .font(.subheadline.weight(.semibold))
                    Text("Tap New Page to write with Apple Pencil, or start a QuickNote on your Even G2 glasses.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 28)
                .listRowBackground(Color.clear)
            }
        }

        if !voices.isEmpty {
            Section(filter == .pages ? "Notebook Pages" : (filter == .voice ? "Voice Notes" : "Saved Notes & Pages")) {
                ForEach(voices) { n in
                    if n.page == true {
                        Button {
                            if isSplit {
                                selection = .page(n.id)
                            } else {
                                selection = .page(n.id)
                                compactPageTarget = CompactPageTarget(noteId: n.id, typed: n.typed ?? "")
                            }
                        } label: {
                            NotebookPageCardView(
                                note: n,
                                isSelected: isSplit && selection == .page(n.id)
                            )
                        }
                        .buttonStyle(.plain)
                        .listRowInsets(EdgeInsets(top: 8, leading: 12, bottom: 8, trailing: 12))
                        .listRowBackground(
                            isSplit && selection == .page(n.id)
                                ? JoatTheme.accent.opacity(0.12)
                                : JoatTheme.card
                        )
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button(role: .destructive) {
                                deleteVoiceNote(n.id)
                            } label: {
                                Label("Delete", systemImage: "trash")
                            }
                        }
                    } else if isSplit {
                        Button {
                            selection = .voice(n.id)
                        } label: {
                            VoiceNoteRowView(note: n)
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(
                            selection == .voice(n.id)
                                ? JoatTheme.accent.opacity(0.12)
                                : JoatTheme.card
                        )
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button(role: .destructive) {
                                deleteVoiceNote(n.id)
                            } label: {
                                Label("Delete", systemImage: "trash")
                            }
                        }
                    } else {
                        NavigationLink(value: NotesSelection.voice(n.id)) {
                            VoiceNoteRowView(note: n)
                        }
                        .listRowBackground(JoatTheme.card)
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button(role: .destructive) {
                                deleteVoiceNote(n.id)
                            } label: {
                                Label("Delete", systemImage: "trash")
                            }
                        }
                    }
                }
            }
        }

        if !texts.isEmpty {
            Section("Quick Text Notes") {
                ForEach(texts) { t in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(t.text)
                            .font(.subheadline)
                        Text(t.updated.formatted(date: .abbreviated, time: .shortened))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 2)
                    .listRowBackground(JoatTheme.card)
                }
            }
        }
    }

    @ViewBuilder
    private func prepSections(isSplit: Bool) -> some View {
        Section {
            Button {
                let id = prep.create()
                if isSplit {
                    selection = .prep(id)
                } else {
                    compactPath = [.prep(id)]
                }
            } label: {
                Label("New prep note", systemImage: "folder.badge.plus")
                    .fontWeight(.medium)
            }
            .listRowBackground(JoatTheme.card)

            if !prep.status.isEmpty {
                Text(prep.status)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .listRowBackground(JoatTheme.card)
            }
        } footer: {
            Text("A prep folder holds typed notes, PDFs, links, and notebook pages for one conversation. Share a PDF or text file to JOAT Brain from any app to add it here.")
        }

        let items = filteredPrepNotes
        if !items.isEmpty {
            Section {
                ForEach(items) { note in
                    HStack(alignment: .top, spacing: 12) {
                        Button {
                            prep.select(note.id)
                        } label: {
                            Image(systemName: note.id == prep.active?.id ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(note.id == prep.active?.id ? JoatTheme.accent : .secondary)
                                .font(.title3)
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel(note.id == prep.active?.id ? "In use" : "Use \(note.title)")

                        if isSplit {
                            Button {
                                selection = .prep(note.id)
                            } label: {
                                prepRowContent(note)
                            }
                            .buttonStyle(.plain)
                        } else {
                            NavigationLink(value: NotesSelection.prep(note.id)) {
                                prepRowContent(note)
                            }
                        }
                    }
                    .listRowBackground(
                        isSplit && selection == .prep(note.id)
                            ? JoatTheme.accent.opacity(0.12)
                            : JoatTheme.card
                    )
                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                        Button(role: .destructive) {
                            if selection == .prep(note.id) { selection = nil }
                            prep.remove(note.id)
                        } label: {
                            Label("Delete", systemImage: "trash")
                        }
                    }
                }
            } header: {
                Text("In Use: Ticked Folder")
            } footer: {
                Text("When a QuickNote starts on the glasses, JOAT uses the active prep folder to surface live talking-point cues.")
            }
        }

        Section {
            Toggle("AI cues during QuickNotes", isOn: $cuesOn)
                .listRowBackground(JoatTheme.card)
        }
    }

    private func prepRowContent(_ note: Prep.Note) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: note.id == prep.active?.id ? "folder.fill" : "folder")
                    .foregroundStyle(note.id == prep.active?.id ? JoatTheme.accent : .secondary)
                Text(note.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Spacer()
                if note.id == prep.active?.id {
                    JoatStatusPill(label: "Active", color: JoatTheme.accent)
                }
            }
            Text(note.summary)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .contentShape(Rectangle())
    }

    @ToolbarContentBuilder
    private func notesToolbar(isSplit: Bool) -> some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            if filter == .prep {
                Button {
                    let id = prep.create()
                    if isSplit {
                        selection = .prep(id)
                    } else {
                        compactPath = [.prep(id)]
                    }
                } label: {
                    Label("New Prep", systemImage: "folder.badge.plus")
                }
            } else {
                Button {
                    if isSplit {
                        createAndSelectNewPageInSplit()
                    } else {
                        compactPageTarget = CompactPageTarget(noteId: nil, typed: "")
                    }
                } label: {
                    Label("New Page", systemImage: "square.and.pencil")
                }
            }
        }
    }

    private var emptyNotesTitle: String {
        if !searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "No matching notes"
        }
        switch filter {
        case .pages: return "No Apple Pencil pages yet"
        case .voice: return "No voice notes yet"
        default: return "No notes yet"
        }
    }

    private func createAndSelectNewPageInSplit() {
        guard let quickNotes = JOATRuntime.shared.brain?.quickNotes else { return }
        let newId = quickNotes.createBlankPage()
        model.reload()
        selection = .page(newId)
    }

    private func deleteVoiceNote(_ id: String) {
        if selection == .page(id) || selection == .voice(id) {
            selection = nil
        }
        JOATRuntime.shared.brain?.quickNotes.deleteNote(id)
        model.reload()
    }

    private func syncRegularSelectionIfNeeded() {
        guard horizontalSizeClass == .regular else { return }
        if filter == .prep {
            if case .prep(let id) = selection, prep.notes.contains(where: { $0.id == id }) {
                return
            }
            selection = (prep.active ?? prep.notes.first).map { .prep($0.id) }
            return
        }

        switch selection {
        case .page(let id) where filteredVoiceNotes.contains(where: { $0.id == id }):
            return
        case .voice(let id) where filteredVoiceNotes.contains(where: { $0.id == id }):
            return
        default:
            if let first = filteredVoiceNotes.first {
                selection = first.page == true ? .page(first.id) : .voice(first.id)
            } else {
                selection = nil
            }
        }
    }

    private func recoverAndReload() {
        if let quickNotes = JOATRuntime.shared.brain?.quickNotes {
            Notebook.recoverIfClosed(quickNotes: quickNotes)
        }
        model.reload()
    }

    #if DEBUG
    private static var debugSeeded = false
    /// JOAT_DEBUG_NOTES_INSIGHTS=1 (with JOAT_DEBUG_NOTEBOOK_SEED=1): the seeded
    /// pages get insights as a model would give them, for Simulator screenshots.
    static func debugSeedInsightsOnce() {
        guard !debugSeeded, ProcessInfo.processInfo.environment["JOAT_DEBUG_NOTES_INSIGHTS"] == "1" else { return }
        debugSeeded = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            guard let qn = JOATRuntime.shared.brain?.quickNotes else { return }
            let typedId = "debug-typed-page-1"
            qn.ensureBlankPage(id: typedId, paperTemplate: PaperTemplate.dotGrid.rawValue)
            qn.savePage(id: typedId, typed: "Standup: firmware 0.1.152 is signed off. Ask Dana for the TestFlight build notes before Thursday.",
                        inkChanged: false, paperTemplate: PaperTemplate.dotGrid.rawValue)
            qn.renameNote(typedId, to: "Standup — Firmware Sign-off")
            qn.debugSetInsights(typedId,
                                summary: "Firmware 0.1.152 is signed off; TestFlight build notes are still needed from Dana before Thursday.",
                                keyPoints: ["Firmware 0.1.152 signed off"], todos: ["Ask Dana for TestFlight build notes by Thursday"])
            qn.debugSetInsights("debug-duo-page-1",
                                summary: "Hardware walkthrough: verify G2 lens alignment and battery telemetry, call Mike Friday at 3 pm about the tag map, and order two spare relays.",
                                keyPoints: ["Verify G2 lens alignment", "Check battery telemetry", "Tag map needs Mike's review"],
                                todos: ["Call Mike about the tag map — Friday 3 pm", "Order two spare relays"])
            JOATRuntime.shared.notes.reload()
        }
    }
    #endif
}

// MARK: - Apple Pencil Page & Voice Note Cards

private struct NotebookPageCardView: View {
    let note: QuickNotes.VoiceNote
    let isSelected: Bool

    private var templateInfo: (label: String, icon: String) {
        switch note.paperTemplate {
        case "ruled": return ("Ruled", "line.3.horizontal")
        case "dotGrid": return ("Dot Grid", "circle.grid.3x3")
        case "squareGrid": return ("Grid", "squareshape.split.3x3")
        default: return ("Blank", "doc")
        }
    }

    private var previewText: String {
        if let line = NotesInsights.rowLine(summary: note.summary, keyPoints: note.keyPoints, paragraphs: note.paragraphs) {
            return line
        }
        if Notebook.reading.contains(note.id) {
            return "Reading handwriting…"
        }
        return "Tap to sketch or write with Apple Pencil"
    }

    var body: some View {
        let thumb = PageThumbnailCache.thumbnail(for: note)
        let cueCount = note.cues?.count ?? 0

        VStack(alignment: .leading, spacing: 10) {
            // Warm `#FAF8F4` stationery preview window
            ZStack(alignment: .topTrailing) {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(JoatTheme.paper)

                if let thumb {
                    Image(uiImage: thumb)
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: 112, alignment: .topLeading)
                        .padding(8)
                } else {
                    StationeryRulePreview(template: note.paperTemplate)
                        .frame(height: 54)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                }

                HStack(spacing: 4) {
                    Image(systemName: templateInfo.icon)
                        .font(.system(size: 9, weight: .bold))
                    Text(templateInfo.label)
                        .font(.system(size: 10, weight: .semibold))
                }
                .foregroundStyle(Color(red: 0.25, green: 0.24, blue: 0.22))
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.white.opacity(0.88), in: Capsule())
                .overlay(Capsule().strokeBorder(JoatTheme.paperBorder, lineWidth: 0.75))
                .padding(8)
            }
            .frame(height: thumb != nil ? 120 : 54)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(JoatTheme.paperBorder, lineWidth: 1)
            )

            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Label {
                        Text(note.title.isEmpty ? "Untitled Page" : note.title)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                    } icon: {
                        Image(systemName: "pencil.and.scribble")
                            .foregroundStyle(JoatTheme.accent)
                            .font(.footnote.weight(.bold))
                    }

                    Spacer()

                    Text(note.updated.formatted(date: .abbreviated, time: .shortened))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }

                NoteSummaryLine(note: note, fallback: previewText)

                if !note.todos.isEmpty || cueCount > 0 || Notebook.reading.contains(note.id) {
                    HStack(spacing: 6) {
                        if Notebook.reading.contains(note.id) {
                            JoatStatusPill(label: "Reading handwriting…", color: JoatTheme.amber, icon: "text.viewfinder")
                        }
                        if !note.todos.isEmpty {
                            JoatStatusPill(
                                label: "\(note.todos.count) \(note.todos.count == 1 ? "reminder" : "reminders")",
                                color: JoatTheme.accent,
                                icon: "checklist"
                            )
                        }
                        if cueCount > 0 {
                            JoatStatusPill(
                                label: "\(cueCount) \(cueCount == 1 ? "cue" : "cues")",
                                color: JoatTheme.accent,
                                icon: "sparkles"
                            )
                        }
                    }
                    .padding(.top, 2)
                }
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }
}

private struct StationeryRulePreview: View {
    let template: String?

    var body: some View {
        GeometryReader { geo in
            Canvas { context, size in
                let strokeColor = Color(red: 0.56, green: 0.62, blue: 0.68).opacity(0.28)
                switch template {
                case "dotGrid":
                    for x in stride(from: 14.0, to: size.width - 8, by: 14.0) {
                        for y in stride(from: 14.0, to: size.height - 6, by: 14.0) {
                            let dot = CGRect(x: x, y: y, width: 2, height: 2)
                            context.fill(Path(ellipseIn: dot), with: .color(strokeColor))
                        }
                    }
                case "squareGrid":
                    var grid = Path()
                    for x in stride(from: 16.0, to: size.width, by: 16.0) {
                        grid.move(to: CGPoint(x: x, y: 0))
                        grid.addLine(to: CGPoint(x: x, y: size.height))
                    }
                    for y in stride(from: 16.0, to: size.height, by: 16.0) {
                        grid.move(to: CGPoint(x: 0, y: y))
                        grid.addLine(to: CGPoint(x: size.width, y: y))
                    }
                    context.stroke(grid, with: .color(strokeColor), lineWidth: 0.6)
                case "ruled":
                    var rules = Path()
                    for y in stride(from: 16.0, to: size.height, by: 14.0) {
                        rules.move(to: CGPoint(x: 0, y: y))
                        rules.addLine(to: CGPoint(x: size.width, y: y))
                    }
                    context.stroke(rules, with: .color(strokeColor), lineWidth: 0.7)
                    var margin = Path()
                    margin.move(to: CGPoint(x: 32, y: 0))
                    margin.addLine(to: CGPoint(x: 32, y: size.height))
                    context.stroke(margin, with: .color(Color.red.opacity(0.18)), lineWidth: 0.7)
                default:
                    break
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
    }
}

/// The 1–2 line summary under a note in the list (typed, handwritten or
/// voice): the AI summary with a sparkle, else its first words, and a
/// subtle "Summarizing…" while one is on its way.
private struct NoteSummaryLine: View {
    let note: QuickNotes.VoiceNote
    let fallback: String

    var body: some View {
        let summary = (note.summary ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let summarizing = JOATRuntime.shared.brain?.quickNotes.isSummarizing(note.id) ?? false
        VStack(alignment: .leading, spacing: 2) {
            if !summary.isEmpty {
                (Text(Image(systemName: "sparkles")).foregroundStyle(JoatTheme.accent) + Text(" ") + Text(summary))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            } else {
                Text(fallback)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            if summarizing {
                Text("Summarizing…")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
    }
}

private struct VoiceNoteRowView: View {
    let note: QuickNotes.VoiceNote

    var body: some View {
        let cueCount = note.cues?.count ?? 0
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Label {
                    Text(note.title.isEmpty ? "Untitled" : note.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                } icon: {
                    Image(systemName: "waveform")
                        .foregroundStyle(JoatTheme.accent)
                        .font(.footnote.weight(.bold))
                }
                Spacer()
                Text(note.updated.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            if let text = NotesInsights.rowLine(summary: note.summary, keyPoints: note.keyPoints, paragraphs: note.paragraphs) {
                NoteSummaryLine(note: note, fallback: text)
            }

            if !note.todos.isEmpty || cueCount > 0 {
                HStack(spacing: 6) {
                    if !note.todos.isEmpty {
                        JoatStatusPill(
                            label: "\(note.todos.count) \(note.todos.count == 1 ? "reminder" : "reminders")",
                            color: JoatTheme.accent,
                            icon: "checklist"
                        )
                    }
                    if cueCount > 0 {
                        JoatStatusPill(
                            label: "\(cueCount) \(cueCount == 1 ? "cue" : "cues")",
                            color: JoatTheme.accent,
                            icon: "sparkles"
                        )
                    }
                }
                .padding(.top, 2)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }
}

// MARK: - Voice / Saved Note Detail & Shared Reminder / Prep Sheets

struct OfferSheet: Identifiable {
    let id = UUID()
    let offer: QuickNotes.ReminderOffer
}

struct NoteDetailView: View {
    let noteId: String
    @ObservedObject var model: NotesModel
    @State private var sheet: OfferSheet?
    @State private var finding = false
    @State private var summarizing = false
    @State private var message = ""
    @State private var editing = false
    @State private var choosingPrep = false

    private var note: QuickNotes.VoiceNote? { model.voice.first { $0.id == noteId } }
    private var quickNotes: QuickNotes? { JOATRuntime.shared.brain?.quickNotes }

    var body: some View {
        if let n = note {
            List {
                if n.page == true { pageSection(n) }
                Section {
                    Text(n.created.formatted(date: .complete, time: .shortened))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Button {
                        findReminders()
                    } label: {
                        Label(finding ? "Finding reminders…" : "Find reminders in this note", systemImage: "checklist")
                    }
                    .disabled(finding)
                    if !message.isEmpty {
                        Text(message).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if let summary = n.summary {
                    Section("Overview") { Text(summary) }
                } else {
                    Section {
                        Button(summarizing ? "Summarizing…" : "Summarize this note") {
                            summarizing = true
                            quickNotes?.summarize(n.id) { r in
                                summarizing = false
                                message = r
                                model.reload()
                            }
                        }
                        .disabled(summarizing)
                    }
                }
                if !n.keyPoints.isEmpty {
                    Section("Key points") { ForEach(n.keyPoints, id: \.self) { Text("• \($0)") } }
                }
                if !n.todos.isEmpty {
                    Section("Next steps") { ForEach(n.todos, id: \.self) { Text("□ \($0)") } }
                }
                if let cues = n.cues, !cues.isEmpty {
                    Section("AI cues") { ForEach(cues, id: \.self) { Text($0).font(.callout) } }
                }
                Section(n.page == true ? "Text" : "Transcript") {
                    if n.paragraphs.isEmpty {
                        Text("No transcript text yet.")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(Array(n.paragraphs.enumerated()), id: \.offset) { Text($0.element) }
                    }
                }
            }
            .navigationTitle(n.title.isEmpty ? "Untitled" : n.title)
            .navigationBarTitleDisplayMode(.inline)
            .sheet(item: $sheet) { s in
                ReminderReviewSheet(offer: s.offer) { result in message = result }
            }
            .fullScreenCover(isPresented: $editing) {
                PageEditorView(noteId: n.id, typed: n.typed ?? "", drawing: PageStore.load(n.id), embedded: false) { _ in
                    model.reload()
                }
            }
            .sheet(isPresented: $choosingPrep) {
                PrepTargetPicker(what: n.title.isEmpty ? "this note" : "“\(n.title)”") { target in addToPrep(n, target) }
            }
        } else {
            ContentUnavailableView("This note is gone", systemImage: "note.text")
        }
    }

    @ViewBuilder
    private func pageSection(_ n: QuickNotes.VoiceNote) -> some View {
        let ink = PageStore.hasInk(n.id)
        let pasted = PageStore.images(n.id)
        Section {
            if let image = PageStore.preview(PageStore.load(n.id)) {
                Image(uiImage: image).resizable().scaledToFit().frame(maxHeight: 420)
            }
            ForEach(Array(pasted.enumerated()), id: \.offset) { _, image in
                Image(uiImage: image).resizable().scaledToFit().frame(maxHeight: 420)
                    .accessibilityLabel("Pasted image")
            }
            Button { editing = true } label: { Label("Open in Notebook", systemImage: "square.and.pencil") }
            if ink || !pasted.isEmpty, n.handwriting == nil {
                if Notebook.reading.contains(n.id) {
                    Label("Reading your page…", systemImage: "hourglass")
                } else if let quickNotes {
                    Button {
                        Notebook.read(n.id, PageStore.load(n.id), quickNotes: quickNotes)
                        model.reload()
                    } label: {
                        Label(ink ? "Read my handwriting" : "Read the pasted images", systemImage: "text.viewfinder")
                    }
                }
            }
            Button {
                guard !n.paragraphs.joined().isEmpty else { message = "This note has no text to use yet."; return }
                choosingPrep = true
            } label: { Label("Add to a prep note…", systemImage: "doc.text.magnifyingglass") }
        }
    }

    private func addToPrep(_ n: QuickNotes.VoiceNote, _ target: String?) {
        let prep = JOATRuntime.shared.prep
        prep.addText(
            n.paragraphs.joined(separator: "\n"),
            title: n.title.isEmpty ? "Note" : n.title,
            source: n.page == true ? "Notebook page" : "Voice note",
            to: target
        )
        let name = target.flatMap { id in prep.notes.first { $0.id == id }?.title } ?? "a new prep note"
        message = "Added to \(name): the AI cues use it when that prep note is in use."
    }

    private func findReminders() {
        guard let quickNotes else { return }
        finding = true
        message = ""
        quickNotes.proposeReminders(noteId) { offer, why in
            finding = false
            if let offer { sheet = OfferSheet(offer: offer) } else { message = why }
        }
    }
}

/// Reminders extracted from a note or page: untick any, adjust text/time, then Add.
struct ReminderReviewSheet: View {
    @State private var offer: QuickNotes.ReminderOffer
    @State private var include: [Bool]
    @State private var adding = false
    @Environment(\.dismiss) private var dismiss
    let done: (String) -> Void

    init(offer: QuickNotes.ReminderOffer, done: @escaping (String) -> Void = { _ in }) {
        _offer = State(initialValue: offer)
        _include = State(initialValue: Array(repeating: true, count: offer.items.count))
        self.done = done
    }

    private var chosen: Int { include.filter { $0 }.count }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("From “\(offer.title)”. Untick any you don't want; change the words or the time.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                ForEach(offer.items.indices, id: \.self) { i in
                    Section {
                        Toggle(isOn: $include[i]) {
                            TextField("Reminder", text: $offer.items[i].title)
                        }
                        if include[i] {
                            Toggle("All day", isOn: $offer.items[i].allDay)
                            DatePicker(
                                "When",
                                selection: $offer.items[i].due,
                                displayedComponents: offer.items[i].allDay ? [.date] : [.date, .hourAndMinute]
                            )
                        }
                    }
                }
            }
            .navigationTitle("Reminders")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(adding ? "Adding…" : "Add \(chosen)") { add() }
                        .disabled(chosen == 0 || adding)
                }
            }
        }
        .tint(JoatTheme.accent)
    }

    private func add() {
        guard let quickNotes = JOATRuntime.shared.brain?.quickNotes else { return }
        adding = true
        var picked = offer
        picked.items = offer.items.indices.filter { include[$0] }.map { offer.items[$0] }
        quickNotes.addReminders(picked) { result in
            adding = false
            done(result)
            dismiss()
        }
    }
}

// MARK: - Prep

/// Standalone Prep view kept accessible for compatibility; inline Prep lives inside `NotesView`.
struct PrepView: View {
    @ObservedObject private var prep = JOATRuntime.shared.prep
    @AppStorage(Cues.enabledKey) private var cuesOn = true
    @State private var path: [String] = []

    var body: some View {
        NavigationStack(path: $path) {
            List {
                Section {
                    Button { path = [prep.create()] } label: { Label("New prep note", systemImage: "folder.badge.plus") }
                    if !prep.status.isEmpty {
                        Text(prep.status).font(.caption).foregroundStyle(.secondary)
                    }
                } footer: {
                    Text("A prep note holds everything for one conversation: typed notes, files, links and notebook pages. "
                         + "Share a PDF or text file to JOAT Brain from any app to add it to one.")
                }
                if !prep.notes.isEmpty {
                    Section {
                        ForEach(prep.notes) { note in
                            HStack(alignment: .top, spacing: 12) {
                                Button { prep.select(note.id) } label: {
                                    Image(systemName: note.id == prep.active?.id ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(note.id == prep.active?.id ? JoatTheme.accent : .secondary)
                                        .font(.title3)
                                }
                                .buttonStyle(.borderless)
                                .accessibilityLabel(note.id == prep.active?.id ? "In use" : "Use \(note.title)")
                                NavigationLink(value: note.id) {
                                    VStack(alignment: .leading, spacing: 3) {
                                        Label(note.title, systemImage: "folder").foregroundStyle(.primary)
                                        Text(note.summary).font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                        .onDelete { offsets in offsets.map { prep.notes[$0].id }.forEach { prep.remove($0) } }
                    } header: {
                        Text("In use: the ticked one")
                    } footer: {
                        Text("On the glasses, + Create new note asks which prep note to use, or Skip & start. "
                             + "On an open QuickNote, new AI cues pop up for a few seconds; tap the note to see the prep and every cue.")
                    }
                }
                Section { Toggle("AI cues during QuickNotes", isOn: $cuesOn) }
            }
            .navigationTitle("Prep")
            .navigationDestination(for: String.self) { id in PrepNoteView(noteId: id) }
        }
    }
}

/// One prep note: its name, what's in it, and its talking points.
struct PrepNoteView: View {
    let noteId: String
    @ObservedObject private var prep = JOATRuntime.shared.prep
    @State private var importing = false
    @State private var typing = false
    @State private var typed = ""
    @State private var name = ""

    private var note: Prep.Note? { prep.notes.first { $0.id == noteId } }

    var body: some View {
        if let note {
            List {
                Section {
                    TextField("Name", text: $name, prompt: Text(note.title))
                        .font(.headline)
                        .accessibilityIdentifier("Name")
                        .onSubmit { prep.rename(noteId, to: name) }
                    if note.id == prep.active?.id {
                        Label("In use", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(JoatTheme.accent)
                    } else {
                        Button { prep.select(noteId) } label: { Label("Use for conversations", systemImage: "circle") }
                    }
                }
                Section {
                    Button { importing = true } label: { Label("Add files…", systemImage: "doc.badge.plus") }
                    PasteButton(payloadType: String.self) { strings in
                        DispatchQueue.main.async { prep.paste(strings.first, url: nil, to: noteId) }
                    }
                    Button { typing = true } label: { Label("Type notes…", systemImage: "square.and.pencil") }
                    if !prep.status.isEmpty {
                        Text(prep.status).font(.caption).foregroundStyle(.secondary)
                    }
                } footer: {
                    Text("Paste takes a link (the page is read) or text. Notebook pages: open the page in Notes › AI Insights › Add to Prep.")
                }
                Section("In this prep note") {
                    if note.items.isEmpty {
                        Text("Nothing yet.").foregroundStyle(.secondary)
                    }
                    ForEach(note.items) { item in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(item.title)
                            Text("\(item.source) · \(item.text.count.formatted()) characters")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .onDelete { offsets in offsets.map { note.items[$0].id }.forEach { prep.removeItem($0, from: noteId) } }
                }
                if !note.points.isEmpty {
                    Section("Talking points, from all of it") {
                        ForEach(note.points, id: \.self) { Text("– \($0)").font(.callout) }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(JoatTheme.surface.ignoresSafeArea())
            .navigationTitle(note.title)
            .navigationBarTitleDisplayMode(.inline)
            .onAppear { name = note.named ? note.title : "" }
            .onChange(of: note.title) { _, new in if note.named { name = new } }
            .onDisappear { if !name.isEmpty, name != note.title { prep.rename(noteId, to: name) } }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.pdf, .text, .html, .rtf], allowsMultipleSelection: true) { result in
                if case .success(let urls) = result { urls.forEach { prep.addFile($0, to: noteId) } }
            }
            .sheet(isPresented: $typing) {
                NavigationStack {
                    TextEditor(text: $typed)
                        .padding()
                        .navigationTitle("Type notes")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { typing = false } }
                            ToolbarItem(placement: .confirmationAction) {
                                Button("Add") { prep.addText(typed, to: noteId); typed = ""; typing = false }
                                    .disabled(typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }
                        }
                }
                .tint(JoatTheme.accent)
            }
        } else {
            ContentUnavailableView("This prep note is gone", systemImage: "folder")
        }
    }
}

/// Which prep note something goes into (a shared file, a notebook page):
/// one of them, or a new one. `chose(nil)`: a new prep note.
struct PrepTargetPicker: View {
    let what: String
    var cancelled: () -> Void = {}
    let chose: (String?) -> Void
    @ObservedObject private var prep = JOATRuntime.shared.prep
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button { chose(nil); dismiss() } label: { Label("New prep note", systemImage: "folder.badge.plus") }
                }
                if !prep.notes.isEmpty {
                    Section("Prep notes") {
                        ForEach(prep.notes) { note in
                            Button { chose(note.id); dismiss() } label: {
                                VStack(alignment: .leading, spacing: 3) {
                                    Label(note.title, systemImage: note.id == prep.active?.id ? "folder.fill" : "folder")
                                    Text(note.id == prep.active?.id ? "In use · \(note.summary)" : note.summary)
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Add \(what) to")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { cancelled(); dismiss() } }
            }
        }
        .tint(JoatTheme.accent)
    }
}

// MARK: - Diagnostics

struct DiagnosticsView: View {
    var embedded: Bool = false

    @ObservedObject private var log = Log.shared
    @ObservedObject private var server = JOATRuntime.shared.server
    @ObservedObject private var keepAlive = JOATRuntime.shared.keepAlive
    @ObservedObject private var stt = JOATRuntime.shared.stt
    @ObservedObject private var endurance = JOATRuntime.shared.endurance
    @ObservedObject private var voice = JOATRuntime.shared.voice
    @StateObject private var selfTest = SelfTest()
    @AppStorage(AudioCheck.keepKey) private var keepNoteAudio = false
    @State private var noteAudio: [URL] = []

    var body: some View {
        if embedded {
            diagnosticsList
        } else {
            NavigationStack {
                diagnosticsList
            }
        }
    }

    private var diagnosticsList: some View {
        List {
            Section {
                Toggle("Save note audio", isOn: $keepNoteAudio)
                ForEach(noteAudio, id: \.self) { url in
                    ShareLink(item: url) {
                        Label(url.deletingPathExtension().lastPathComponent, systemImage: "waveform")
                    }
                }
                .onDelete { rows in
                    rows.map { noteAudio[$0] }.forEach { try? FileManager.default.removeItem(at: $0) }
                    noteAudio = AudioCheck.saved()
                }
                ShareLink(item: Log.fileURL) { Label("Share the log", systemImage: "doc.text") }
            } header: {
                Text("The glasses' mic")
            } footer: {
                Text("While a note records, the log gets the glasses' sound level every second. With Save note audio on, each note's audio is kept on this phone too, as a file to share. Swipe a file to delete it.")
            }
            Section("Glasses link") {
                LabeledContent("Listener", value: server.state)
                LabeledContent("Plugin origin", value: server.lastOrigin)
                LabeledContent("Authenticated", value: server.authenticated ? "yes" : "no")
                LabeledContent("Heartbeat", value: "\(server.pongs) pongs, last \(server.lastPongMs) ms")
                LabeledContent("Glasses audio", value: "\(server.audioFrames) frames")
                HStack {
                    Button("Self-test") { selfTest.run(port: server.port) }.buttonStyle(.borderless)
                    Spacer()
                    Text(selfTest.result).foregroundStyle(.secondary)
                }
            }
            Section("Speech") {
                LabeledContent("Engine", value: stt.engine)
                LabeledContent("Hearing", value: stt.lastVolatile.isEmpty ? "—" : stt.lastVolatile)
                LabeledContent("Last sentence", value: stt.lastFinal.isEmpty ? "—" : stt.lastFinal)
                LabeledContent("Finalised after", value: stt.lastLatencyMs.map { "\($0) ms" } ?? "—")
                Button("Speak a test line") { voice.say("JOAT Brain, speaking from the phone.") }
            }
            Section("Keep-alive") {
                LabeledContent("Active", value: keepAlive.active ? "yes (\(keepAlive.mode))" : "no (\(keepAlive.mode))")
                LabeledContent("Route", value: keepAlive.route)
                HStack {
                    Button("Start") { keepAlive.start() }.buttonStyle(.borderless)
                    Spacer()
                    Button("Stop", role: .destructive) { keepAlive.stop() }.buttonStyle(.borderless)
                }
            }
            Section("Uptime") {
                Text(endurance.summary).font(.system(.caption, design: .monospaced))
            }
            Section {
                ForEach(Array(log.lines.enumerated().reversed()), id: \.offset) { _, line in
                    Text(line).font(.system(.caption, design: .monospaced))
                }
            } header: {
                HStack { Text("Log"); Spacer(); Button("Clear") { log.clear() }.font(.caption) }
            }
        }
        .scrollContentBackground(.hidden)
        .background(JoatTheme.surface.ignoresSafeArea())
        .navigationTitle("Diagnostics")
        .onAppear { noteAudio = AudioCheck.saved() }
        .refreshable { noteAudio = AudioCheck.saved() }
    }
}

// MARK: - Agents

/// Configures the home Mac's Agent Terminal (Claude Code, Codex, Antigravity)
/// over Tailscale so the Mac mini stays headless while the glasses stay paired
/// to JOAT Brain on this iPhone.
struct AgentsView: View {
    @ObservedObject var model: MacTerminal = JOATRuntime.shared.macTerminal
    @State private var loginCode = ""
    @State private var copiedNote: String?
    @State private var workspaceTarget: WorkspaceSheetTarget?
    @State private var showingGeminiKeySheet = false
    @State private var addAccountTarget: AddAccountTarget?
    @State private var removalTarget: PendingAccountRemoval?

    private var readyAgents: [MacTerminal.AgentItem] {
        model.state.agents.filter { $0.installed && $0.loggedIn }
    }

    var body: some View {
        NavigationStack {
            List {
                connectionSection
                if let login = model.state.login {
                    loginSection(login)
                }
                ForEach(model.state.agents) { agent in
                    agentSection(agent)
                }
                if model.state.assistantSeat != nil || !(model.state.assistantOptions ?? []).isEmpty {
                    assistantSection
                }
                if let groups = model.state.accounts, !groups.isEmpty {
                    ForEach(groups) { group in
                        accountGroupSection(group)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .background(JoatTheme.surface.ignoresSafeArea())
            .navigationTitle("Agents")
            .toolbar {
                if model.configured {
                    ToolbarItem(placement: .primaryAction) {
                        Button {
                            Task { await model.refresh(sendRefreshAction: true) }
                        } label: {
                            Label("Refresh", systemImage: "arrow.clockwise")
                        }
                        .disabled(model.isPerformingAction)
                    }
                }
            }
            .task {
                if model.state.agents.isEmpty {
                    await model.refresh()
                } else {
                    await model.fetchState(quiet: true)
                }
            }
            .refreshable {
                await model.refresh(sendRefreshAction: true)
            }
            .onAppear { model.setViewVisible(true) }
            .onDisappear { model.setViewVisible(false) }
            .sheet(item: $workspaceTarget) { target in
                WorkspaceEditorSheet(target: target, model: model)
            }
            .sheet(isPresented: $showingGeminiKeySheet) {
                GeminiKeySheet(model: model)
            }
            .sheet(item: $addAccountTarget) { target in
                AddAccountSheet(target: target, model: model)
            }
            .confirmationDialog(
                removalTarget.map { "Remove “\($0.label)”?" } ?? "Remove account?",
                isPresented: Binding(
                    get: { removalTarget != nil },
                    set: { if !$0 { removalTarget = nil } }
                ),
                titleVisibility: .visible
            ) {
                if let target = removalTarget {
                    Button("Remove “\(target.label)”", role: .destructive) {
                        removalTarget = nil
                        Task { await model.removeAccount(provider: target.provider, profileId: target.profileId) }
                    }
                }
                Button("Cancel", role: .cancel) { removalTarget = nil }
            } message: {
                Text("Signs out and removes this saved CLI profile on your Mac.")
            }
        }
    }

    // MARK: Sections

    @ViewBuilder
    private var connectionSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 10) {
                if !model.configured {
                    JoatHeroCard(
                        eyebrow: "Tailscale Terminal",
                        title: "Agent Terminal not linked",
                        subtitle: "Rebuild JOAT Brain from your Mac with scripts/build_ios.sh while JOAT and Tailscale are running.",
                        icon: "terminal",
                        statusColor: JoatTheme.amber
                    ) {
                        JoatStatusPill(label: "Unconfigured", color: JoatTheme.amber, icon: "link.badge.plus")
                    }
                } else {
                    switch model.connectionState {
                    case .loading where model.state.agents.isEmpty:
                        JoatHeroCard(
                            eyebrow: "Tailscale Terminal",
                            title: "Connecting to your Mac…",
                            subtitle: "Reaching \(model.hostLabel ?? "Tailscale") for Claude Code, Codex, and Antigravity status.",
                            icon: "terminal.fill",
                            statusColor: JoatTheme.accent
                        ) {
                            HStack(spacing: 8) {
                                ProgressView().controlSize(.mini)
                                if let host = model.hostLabel {
                                    JoatStatusPill(label: host, color: JoatTheme.accent, icon: "network")
                                }
                            }
                        }

                    case .failed(let message) where model.state.agents.isEmpty:
                        JoatHeroCard(
                            eyebrow: "Tailscale Terminal",
                            title: "Mac unreachable",
                            subtitle: message,
                            icon: "exclamationmark.triangle.fill",
                            statusColor: JoatTheme.amber,
                            actionTitle: "Retry",
                            action: { Task { await model.refresh() } }
                        ) {
                            if let host = model.hostLabel {
                                JoatStatusPill(label: host, color: JoatTheme.amber, icon: "network.slash")
                            }
                        }

                    default:
                        JoatHeroCard(
                            eyebrow: "Tailscale Terminal",
                            title: readyAgents.isEmpty
                                ? "Mac connected · No agents ready"
                                : "Mac connected · \(readyAgents.count) \(readyAgents.count == 1 ? "agent" : "agents") ready",
                            subtitle: readyDetail,
                            icon: "terminal.fill",
                            statusColor: readyAgents.isEmpty ? JoatTheme.amber : JoatTheme.accent
                        ) {
                            ScrollView(.horizontal, showsIndicators: false) {
                                HStack(spacing: 8) {
                                    if let host = model.hostLabel {
                                        JoatStatusPill(label: host, color: JoatTheme.accent, icon: "network")
                                    }
                                    JoatStatusPill(
                                        label: "\(readyAgents.count)/\(max(model.state.agents.count, readyAgents.count)) Ready",
                                        color: readyAgents.isEmpty ? JoatTheme.amber : JoatTheme.accent,
                                        icon: "bolt.horizontal.fill"
                                    )
                                    if let seat = model.state.assistantSeat, !seat.isEmpty {
                                        JoatStatusPill(label: seatTitle(seat), color: JoatTheme.accent, icon: "sparkles")
                                    }
                                }
                            }
                        }
                    }
                }

                if let err = model.actionError, !err.isEmpty {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(JoatTheme.amber)
                        Text(err).font(.footnote)
                        Spacer()
                        Button { model.actionError = nil } label: {
                            Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                        }
                        .buttonStyle(.borderless)
                    }
                    .joatCardContainer(cornerRadius: 12, padding: 10)
                } else if let note = copiedNote ?? model.notice, !note.isEmpty {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(JoatTheme.accent)
                        Text(note).font(.footnote).foregroundStyle(.secondary)
                        Spacer()
                        Button {
                            copiedNote = nil
                            model.notice = nil
                        } label: {
                            Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary)
                        }
                        .buttonStyle(.borderless)
                    }
                    .joatCardContainer(cornerRadius: 12, padding: 10)
                }
            }
            .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)
        } footer: {
            if !model.configured {
                Text("build_ios.sh pairs this iPhone with your Mac's /terminal endpoint over Tailscale automatically—no manual hostnames or tokens needed.")
            } else if let host = model.hostLabel {
                Text("Connected via \(host). Open Agent Terminal from the glasses menu to pick a session and talk hands-free.")
            }
        }
    }

    private var readyDetail: String {
        if model.state.agents.isEmpty {
            return "Waiting for agent status from \(model.hostLabel ?? "your Mac")."
        }
        if readyAgents.isEmpty {
            return "Sign in or add a key below so agents can take instructions on the glasses."
        }
        let names = readyAgents.map(\.displayTitle).joined(separator: ", ")
        return "\(names) ready for the glasses."
    }

    @ViewBuilder
    private func loginSection(_ login: MacTerminal.LoginProgress) -> some View {
        let providerLabel = MacTerminal.ProviderAccountGroup.defaultTitle(for: login.provider)
        Section {
            HStack {
                Label(loginTitle(for: login, provider: providerLabel), systemImage: loginIcon(for: login))
                    .font(.body.weight(.semibold))
                Spacer()
                if login.isActive {
                    ProgressView().controlSize(.small)
                }
            }
            if !login.message.isEmpty {
                Text(login.message).font(.footnote).foregroundStyle(.secondary)
            }
            if !login.isFinished {
                if let rawURL = login.url, let url = URL(string: rawURL) {
                    Button {
                        UIApplication.shared.open(url)
                    } label: {
                        Label("Open sign-in page in Safari", systemImage: "safari")
                    }
                    Button {
                        UIPasteboard.general.string = rawURL
                        copiedNote = "Copied sign-in link."
                    } label: {
                        Label("Copy sign-in link", systemImage: "doc.on.doc")
                    }
                }
                if let code = login.code, !code.isEmpty {
                    LabeledContent("Device code") {
                        Text(code)
                            .font(.system(.body, design: .monospaced).weight(.bold))
                            .textSelection(.enabled)
                    }
                    Button {
                        UIPasteboard.general.string = code
                        copiedNote = "Copied device code \(code)."
                    } label: {
                        Label("Copy device code", systemImage: "doc.on.doc")
                    }
                }
                if login.needsCode {
                    TextField("Paste sign-in code from browser", text: $loginCode)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.system(.body, design: .monospaced))
                    Button {
                        let submitted = loginCode
                        Task {
                            if await model.submitLoginCode(submitted) {
                                loginCode = ""
                            }
                        }
                    } label: {
                        Label("Submit code", systemImage: "checkmark.circle.fill")
                    }
                    .disabled(loginCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.isPerformingAction)
                }
                Button(role: .destructive) {
                    Task { await model.cancelLogin() }
                } label: {
                    Label("Cancel sign-in", systemImage: "xmark.circle")
                }
            } else {
                Button("Dismiss") {
                    loginCode = ""
                    model.dismissFinishedLogin()
                }
            }
        } header: {
            Text("CLI Sign-In")
        }
    }

    @ViewBuilder
    private func agentSection(_ agent: MacTerminal.AgentItem) -> some View {
        let badge = agentBadge(agent)
        let choices = modelChoices(for: agent)
        Section {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: badge.icon)
                    .foregroundStyle(badge.color)
                    .font(.title3)
                VStack(alignment: .leading, spacing: 2) {
                    Text(agent.displayTitle).font(.body.weight(.semibold))
                    if !agent.statusDetail.isEmpty {
                        Text(agent.statusDetail).font(.caption).foregroundStyle(.secondary)
                    } else if !agent.installed {
                        Text("Not installed on the Mac.").font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                JoatStatusPill(label: badge.label, color: badge.color)
            }
            .padding(.vertical, 2)

            if agent.installed {
                LabeledContent("Active session") {
                    Text(agent.activeSessionTitle ?? (agent.busy ? "Working…" : "Idle"))
                        .foregroundStyle(agent.activeSessionTitle != nil || agent.busy ? .primary : .secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }

                if !choices.isEmpty {
                    Picker("Model", selection: modelBinding(for: agent)) {
                        ForEach(choices, id: \.self) { option in
                            Text(option).tag(option)
                        }
                    }
                }

                Button {
                    workspaceTarget = WorkspaceSheetTarget(
                        provider: agent.provider,
                        title: agent.displayTitle,
                        cwd: agent.cwd
                    )
                } label: {
                    LabeledContent("Workspace") {
                        Text(shortPath(agent.cwd))
                            .font(.system(.footnote, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                .foregroundStyle(.primary)

                if agent.loggedIn {
                    Button {
                        Task { await model.newSession(lane: agent.name) }
                    } label: {
                        Label("Start new session", systemImage: "plus.bubble")
                    }
                    .disabled(model.isPerformingAction)
                }
            }

            if agent.provider.lowercased() == "antigravity" {
                Button {
                    showingGeminiKeySheet = true
                } label: {
                    Label(agent.loggedIn ? "Rotate Gemini API key…" : "Set Gemini API key…", systemImage: "key.fill")
                }
            } else if agent.installed && !agent.loggedIn {
                Button {
                    Task { await model.startLogin(provider: agent.provider) }
                } label: {
                    Label("Sign in on this iPhone…", systemImage: "person.badge.key")
                }
                .disabled(model.isPerformingAction)
            }
        } header: {
            Text(agent.displayTitle)
        }
    }

    private var assistantSection: some View {
        let choices = assistantChoices
        return Section {
            Picker("Assistant seat", selection: assistantSeatBinding) {
                ForEach(choices, id: \.self) { seat in
                    Text(seatTitle(seat)).tag(seat)
                }
            }
        } header: {
            Text("Mac Voice Assistant")
        } footer: {
            Text("Chooses which subscription answers general voice questions routed to your Mac.")
        }
    }

    @ViewBuilder
    private func accountGroupSection(_ group: MacTerminal.ProviderAccountGroup) -> some View {
        Section {
            if !group.installed {
                Text("\(group.title) CLI is not installed on the Mac.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(group.profiles) { profile in
                    HStack(alignment: .center, spacing: 10) {
                        Image(systemName: profile.active ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(profile.active ? JoatTheme.accent : .secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(profile.label).font(.body.weight(profile.active ? .semibold : .regular))
                            let sub = profileSubtitle(profile)
                            if !sub.isEmpty {
                                Text(sub).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        let pBadge = profileBadge(profile.state)
                        JoatStatusPill(label: pBadge.label, color: pBadge.color)
                        Menu {
                            if !profile.active {
                                Button {
                                    Task { await model.selectAccount(provider: group.provider, profileId: profile.profileId) }
                                } label: {
                                    Label("Use this account", systemImage: "checkmark.circle")
                                }
                            }
                            Button {
                                signInProfile(profile, in: group)
                            } label: {
                                Label("Sign in…", systemImage: "person.badge.key")
                            }
                            if !profile.isDefault {
                                Divider()
                                Button(role: .destructive) {
                                    removalTarget = PendingAccountRemoval(
                                        provider: group.provider,
                                        profileId: profile.profileId,
                                        label: profile.label
                                    )
                                } label: {
                                    Label("Remove account", systemImage: "trash")
                                }
                            }
                        } label: {
                            Image(systemName: "ellipsis.circle")
                                .font(.title3)
                                .foregroundStyle(.secondary)
                                .padding(.leading, 4)
                        }
                        .buttonStyle(.borderless)
                    }
                    .padding(.vertical, 2)
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        if !profile.isDefault {
                            Button(role: .destructive) {
                                removalTarget = PendingAccountRemoval(
                                    provider: group.provider,
                                    profileId: profile.profileId,
                                    label: profile.label
                                )
                            } label: {
                                Label("Remove", systemImage: "trash")
                            }
                        }
                    }
                }
                Button {
                    addAccountTarget = AddAccountTarget(provider: group.provider, title: group.title)
                } label: {
                    Label("Add \(group.title) account…", systemImage: "person.crop.circle.badge.plus")
                }
            }
        } header: {
            Text("\(group.title) Accounts")
        }
    }

    // MARK: Helpers

    private func modelChoices(for agent: MacTerminal.AgentItem) -> [String] {
        var list: [String] = []
        if !agent.currentModel.isEmpty { list.append(agent.currentModel) }
        for opt in agent.modelOptions where !opt.isEmpty && !list.contains(opt) {
            list.append(opt)
        }
        return list
    }

    private func modelBinding(for agent: MacTerminal.AgentItem) -> Binding<String> {
        Binding(
            get: {
                if !agent.currentModel.isEmpty { return agent.currentModel }
                return agent.modelOptions.first ?? ""
            },
            set: { chosen in
                guard !chosen.isEmpty, chosen != agent.currentModel else { return }
                Task { await model.setModel(provider: agent.provider, model: chosen) }
            }
        )
    }

    private var assistantChoices: [String] {
        var list: [String] = []
        let raw = model.state.assistantOptions ?? ["claude", "codex", "local"]
        for opt in raw where !opt.isEmpty && !list.contains(opt) {
            list.append(opt)
        }
        if let current = model.state.assistantSeat, !current.isEmpty, !list.contains(current) {
            list.insert(current, at: 0)
        }
        return list
    }

    private var assistantSeatBinding: Binding<String> {
        Binding(
            get: { model.state.assistantSeat ?? assistantChoices.first ?? "claude" },
            set: { chosen in
                guard !chosen.isEmpty, chosen != model.state.assistantSeat else { return }
                Task { await model.setAssistantSeat(chosen) }
            }
        )
    }

    private func seatTitle(_ seat: String) -> String {
        switch seat.lowercased() {
        case "claude": return "Claude Code"
        case "codex": return "Codex"
        case "local", "mlx": return "Local MLX"
        default: return seat.capitalized
        }
    }

    private func signInProfile(_ profile: MacTerminal.AccountProfileItem, in group: MacTerminal.ProviderAccountGroup) {
        Task {
            if !profile.active {
                let ok = await model.selectAccount(provider: group.provider, profileId: profile.profileId)
                guard ok else { return }
            }
            _ = await model.startLogin(provider: group.provider)
        }
    }

    private func shortPath(_ path: String) -> String {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "Default" }
        if let range = trimmed.range(of: #"^/Users/[^/]+"#, options: .regularExpression) {
            return "~" + trimmed[range.upperBound...]
        }
        return trimmed
    }

    private func profileSubtitle(_ profile: MacTerminal.AccountProfileItem) -> String {
        let who = profile.email ?? (profile.isDefault ? "This Mac's default login" : "")
        return [who, profile.plan ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    private func agentBadge(_ agent: MacTerminal.AgentItem) -> (label: String, icon: String, color: Color) {
        if !agent.installed {
            return ("Not installed", "xmark.circle.fill", .secondary)
        }
        if !agent.loggedIn {
            let label = agent.provider.lowercased() == "antigravity" ? "Needs API key" : "Signed out"
            return (label, "exclamationmark.circle.fill", JoatTheme.amber)
        }
        if agent.pendingApprovals > 0 {
            return ("Approval (\(agent.pendingApprovals))", "hand.raised.circle.fill", JoatTheme.amber)
        }
        if agent.busy {
            return ("Working", "bolt.horizontal.circle.fill", JoatTheme.accent)
        }
        return ("Ready", "checkmark.circle.fill", JoatTheme.accent)
    }

    private func profileBadge(_ state: String) -> (label: String, color: Color) {
        switch state.lowercased() {
        case "logged_in": return ("Signed in", JoatTheme.accent)
        case "limited": return ("Limit", JoatTheme.amber)
        case "logged_out": return ("Signed out", JoatTheme.amber)
        case "missing": return ("Not installed", .secondary)
        default: return ("Checking", .secondary)
        }
    }

    private func loginTitle(for login: MacTerminal.LoginProgress, provider: String) -> String {
        switch login.stage {
        case "done": return "\(provider) signed in"
        case "failed": return "\(provider) sign-in failed"
        case "cancelled": return "Sign-in cancelled"
        default: return "Sign in to \(provider)"
        }
    }

    private func loginIcon(for login: MacTerminal.LoginProgress) -> String {
        switch login.stage {
        case "done": return "checkmark.circle.fill"
        case "failed", "cancelled": return "exclamationmark.circle.fill"
        default: return "person.badge.key.fill"
        }
    }
}

private struct WorkspaceSheetTarget: Identifiable {
    let provider: String
    let title: String
    let cwd: String
    var id: String { provider }
}

private struct AddAccountTarget: Identifiable {
    let provider: String
    let title: String
    var id: String { provider }
}

private struct PendingAccountRemoval: Identifiable {
    let provider: String
    let profileId: String
    let label: String
    var id: String { "\(provider):\(profileId)" }
}

private struct WorkspaceEditorSheet: View {
    let target: WorkspaceSheetTarget
    @ObservedObject var model: MacTerminal
    @State private var cwd: String
    @State private var saving = false
    @Environment(\.dismiss) private var dismiss

    init(target: WorkspaceSheetTarget, model: MacTerminal) {
        self.target = target
        self.model = model
        _cwd = State(initialValue: target.cwd)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("~/Developer/project", text: $cwd)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.system(.body, design: .monospaced))
                } header: {
                    Text("Mac folder path")
                } footer: {
                    Text("Directory on your Mac where \(target.title) runs new Agent Terminal sessions.")
                }
                if let err = model.actionError, !err.isEmpty {
                    Section {
                        Text(err).font(.footnote).foregroundStyle(JoatTheme.amber)
                    }
                }
            }
            .navigationTitle("\(target.title) Workspace")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Saving…" : "Save") {
                        saving = true
                        Task {
                            let ok = await model.setCwd(provider: target.provider, cwd: cwd)
                            saving = false
                            if ok { dismiss() }
                        }
                    }
                    .disabled(cwd.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || saving)
                }
            }
        }
        .tint(JoatTheme.accent)
    }
}

/// Write-only Gemini API key entry sheet for Antigravity on the Mac.
private struct GeminiKeySheet: View {
    @ObservedObject var model: MacTerminal
    @State private var key = ""
    @State private var saving = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Paste Gemini API key", text: $key)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Gemini API Key")
                } footer: {
                    Text("Sent over Tailscale to your Mac and saved owner-only (0600) for Antigravity. Write-only: the Mac never sends this key back.")
                }
                if let err = model.actionError, !err.isEmpty {
                    Section {
                        Text(err).font(.footnote).foregroundStyle(JoatTheme.amber)
                    }
                }
            }
            .navigationTitle("Antigravity Key")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Saving…" : "Save") {
                        saving = true
                        let entered = key
                        Task {
                            let ok = await model.setGeminiKey(entered)
                            saving = false
                            if ok {
                                key = ""
                                dismiss()
                            }
                        }
                    }
                    .disabled(key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || saving)
                }
            }
        }
        .tint(JoatTheme.accent)
    }
}

private struct AddAccountSheet: View {
    let target: AddAccountTarget
    @ObservedObject var model: MacTerminal
    @State private var label = ""
    @State private var saving = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Account label (e.g. Work, Personal)", text: $label)
                } header: {
                    Text("Profile Name")
                } footer: {
                    Text("Creates an isolated \(target.title) CLI home on your Mac and starts sign-in on this iPhone.")
                }
                if let err = model.actionError, !err.isEmpty {
                    Section {
                        Text(err).font(.footnote).foregroundStyle(JoatTheme.amber)
                    }
                }
            }
            .navigationTitle("New \(target.title) Account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Adding…" : "Add") {
                        saving = true
                        Task {
                            let ok = await model.addAccount(provider: target.provider, label: label)
                            saving = false
                            if ok { dismiss() }
                        }
                    }
                    .disabled(label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || saving)
                }
            }
        }
        .tint(JoatTheme.accent)
    }
}

struct Step: View {
    let n: Int
    let text: String
    init(n: Int, _ text: String) { self.n = n; self.text = text }
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Text("\(n).").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
            Text(text).font(.callout)
        }
    }
}
