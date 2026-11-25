import Foundation

/// The way back to the glasses: one downlink JSON object to every
/// authenticated plugin. LoopbackServer implements it.
protocol PluginLink: AnyObject {
    func send(_ object: [String: Any])
}

/// One feature ported from the Mac engine (apps/mac/joat). It answers the
/// same uplinks with the same downlinks the Mac does, so the plugin's pages
/// render unchanged.
protocol BrainFeature: AnyObject {
    var name: String { get }
    /// Uplink kinds this feature wants. `input` is shared: a feature that
    /// takes it must ignore events for pages it does not own.
    var uplinkKinds: Set<String> { get }
    /// A plugin just authenticated: push what the Mac pushes on connect.
    func pluginConnected()
    func handle(kind: String, message: [String: Any])
}

/// Hands each uplink to the features that own its kind, on the main queue.
/// Anything nobody owns is counted and logged, so a live session shows which
/// Mac behaviour the phone still lacks.
final class FeatureRouter {
    private var features: [BrainFeature] = []
    private var unhandled: [String: Int] = [:]

    func register(_ feature: BrainFeature) {
        features.append(feature)
        Log.add("feature registered: \(feature.name) owns \(feature.uplinkKinds.sorted())")
    }

    func pluginConnected() {
        DispatchQueue.main.async { self.features.forEach { $0.pluginConnected() } }
    }

    func route(kind: String, message: [String: Any], raw: String) {
        let deliver = { @MainActor in
            if kind == "device" {
                BleDiagnostics.shared.observeDevice(message)
                // Non-glasses Even accessories (e.g. Even R1 smart ring) share SDK
                // `onDeviceStatusChanged` with `wearing: false` and `battery: 0` defaults.
                // Record them in BLE diagnostics, never as G2 doff/don!
                if message.string("deviceRole") == "other" {
                    return
                }
            } else if kind == "policy" {
                BleDiagnostics.shared.observePolicy(message)
            }
            let owners = self.features.filter { $0.uplinkKinds.contains(kind) }
            guard !owners.isEmpty else {
                let n = (self.unhandled[kind] ?? 0) + 1
                self.unhandled[kind] = n
                if n == 1 || n % 25 == 0 {
                    Log.add("unhandled uplink '\(kind)' ×\(n): \(raw.prefix(200))")
                }
                return
            }
            owners.forEach { $0.handle(kind: kind, message: message) }
        }
        if Thread.isMainThread {
            MainActor.assumeIsolated { deliver() }
        } else {
            DispatchQueue.main.async { deliver() }
        }
    }
}

/// User-configurable microphone transport mode (`joat.mic.mode`), synced live
/// with the Even Hub plugin so the wearer can eliminate 24/7 G2 BLE audio
/// interference with the Even R1 ring, AirPods, or car Bluetooth in one tap.
enum MicModeChoice: String, CaseIterable, Identifiable {
    case glasses = "glasses"
    case onDemand = "on_demand"
    case phone = "phone"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .glasses: return "Always On (Glasses Mic)"
        case .onDemand: return "Hold to Talk (R1-Safe)"
        case .phone: return "Phone Mic (R1-Safe)"
        }
    }
}

enum MicResumeChoice: String, CaseIterable, Identifiable {
    case auto = "auto"
    case confirm = "confirm"
    case mac = "mac"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .auto: return "Resume Automatically"
        case .confirm: return "Ask on Glasses First"
        case .mac: return "Brain Decides"
        }
    }
}

/// Live Bluetooth, R1 Ring, G2 Mic Re-arm, and Classic-BT A2DP telemetry +
/// A/B isolation settings surfaced in `HomeView` and `DiagnosticsView`.
@MainActor
final class BleDiagnostics: ObservableObject {
    static let shared = BleDiagnostics()
    static let micModeKey = "joat.mic.mode"
    static let resumePolicyKey = "joat.mic.resume"

    nonisolated static func currentMicModeRaw() -> String {
        let raw = UserDefaults.standard.string(forKey: micModeKey) ?? "glasses"
        return ["glasses", "phone", "on_demand"].contains(raw) ? raw : "glasses"
    }

    nonisolated static func currentResumePolicyRaw() -> String {
        let raw = UserDefaults.standard.string(forKey: resumePolicyKey) ?? "auto"
        return ["auto", "confirm", "mac"].contains(raw) ? raw : "auto"
    }

    @Published var micMode: MicModeChoice = MicModeChoice(
        rawValue: UserDefaults.standard.string(forKey: BleDiagnostics.micModeKey) ?? "glasses"
    ) ?? .glasses {
        didSet {
            UserDefaults.standard.set(micMode.rawValue, forKey: Self.micModeKey)
            if !suppressSync { pushPolicyToActiveServer() }
        }
    }

    @Published var resumeChoice: MicResumeChoice = MicResumeChoice(
        rawValue: UserDefaults.standard.string(forKey: BleDiagnostics.resumePolicyKey) ?? "auto"
    ) ?? .auto {
        didSet {
            UserDefaults.standard.set(resumeChoice.rawValue, forKey: Self.resumePolicyKey)
            if !suppressSync { pushPolicyToActiveServer() }
        }
    }

    @Published private(set) var glassesSnTail: String = "—"
    @Published private(set) var glassesConnectState: String = "—"
    @Published private(set) var otherDeviceEvents: Int = 0
    @Published private(set) var lastOtherDeviceSummary: String = "None recorded"
    @Published private(set) var ringTouches: Int = 0
    @Published private(set) var micRearms: Int = 0
    @Published private(set) var lastRearmReason: String = "—"
    @Published private(set) var routeChanges: Int = 0
    @Published private(set) var lastRouteSummary: String = "—"
    @Published private(set) var bluetoothAudioActive: Bool = false

    weak var activeLink: PluginLink?
    var isActiveLinkConnected: (() -> Bool)?
    private var suppressSync = false
    private let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    func pushPolicyToActiveServer() {
        guard let link = activeLink, isActiveLinkConnected?() ?? true else { return }
        syncPolicy(to: link)
    }

    func syncPolicy(to link: PluginLink) {
        link.send([
            "kind": "policy.set",
            "resume": resumeChoice.rawValue,
            "micMode": micMode.rawValue,
        ])
        Log.add("tx policy.set resume=\(resumeChoice.rawValue) micMode=\(micMode.rawValue)")
    }

    func observePolicy(_ message: [String: Any]) {
        suppressSync = true
        defer { suppressSync = false }
        if let m = message.string("micMode"), let parsed = MicModeChoice(rawValue: m), parsed != micMode {
            micMode = parsed
        }
        if let r = message.string("resume"), let parsed = MicResumeChoice(rawValue: r),
           UserDefaults.standard.object(forKey: Self.resumePolicyKey) != nil,
           parsed != resumeChoice {
            resumeChoice = parsed
        }
    }

    func observeDevice(_ message: [String: Any]) {
        let role = message.string("deviceRole")
        let tail = message.string("snTail")
        let conn = message.string("connect") ?? "unknown"
        if role == "other" {
            otherDeviceEvents += 1
            let batteryNote = message.int("battery").map { " bat=\($0)%" } ?? ""
            let label = "\(stamp.string(from: Date())) · ..\(tail ?? "ring") \(conn)\(batteryNote)"
            lastOtherDeviceSummary = label
            Log.add("ble accessory (\(otherDeviceEvents)): \(label)")
            return
        }
        if let tail, !tail.isEmpty {
            glassesSnTail = "..\(tail)"
        }
        if message["wearing"] != nil || role == "glasses" {
            let wornText = message.bool("wearing").map { $0 ? "worn" : "off-head" } ?? ""
            glassesConnectState = [conn, wornText].filter { !$0.isEmpty }.joined(separator: " · ")
        }
        if let rearm = message.string("rearmReason"), !rearm.isEmpty {
            micRearms += 1
            lastRearmReason = "\(stamp.string(from: Date())) · \(rearm)"
            Log.add("ble mic re-arm (\(micRearms)): \(rearm)")
        }
        if let touches = message.int("ringTouches") {
            ringTouches = touches
        }
    }

    func recordAudioRoute(route: String, reason: String?, bluetoothActive: Bool) {
        bluetoothAudioActive = bluetoothActive
        if let reason {
            routeChanges += 1
            lastRouteSummary = "\(stamp.string(from: Date())) · \(reason) (\(route))"
        } else if lastRouteSummary == "—" {
            lastRouteSummary = route
        }
    }

    func resetCounters() {
        otherDeviceEvents = 0
        lastOtherDeviceSummary = "None recorded"
        ringTouches = 0
        micRearms = 0
        lastRearmReason = "—"
        routeChanges = 0
    }
}

/// The mic after the glasses come back on (__main__.py on_device). Tracks
/// real `mic` state from `kind: "device"`, syncs `JOAT Brain`'s policy on
/// connect, and reopens listening under `"auto"` or `"mac"` unless the wearer
/// chose `"on_demand"` (R1-safe hold-to-talk) or `"confirm"`.
final class MicResume: BrainFeature {
    let name = "mic resume"
    let uplinkKinds: Set<String> = ["device", "policy"]
    private let link: PluginLink
    private var policy: String?
    private var micMode: String = BleDiagnostics.currentMicModeRaw()
    private var micOpen: Bool?
    private var wearing: Bool?

    init(link: PluginLink) { self.link = link }

    func pluginConnected() {
        micOpen = nil
        wearing = nil
        micMode = BleDiagnostics.currentMicModeRaw()
    }

    func handle(kind: String, message: [String: Any]) {
        if kind == "policy" {
            policy = message.string("resume")
            if let m = message.string("micMode") {
                micMode = m
            }
            Log.add("mic resume policy: \(policy ?? "?") (micMode=\(micMode))")
            return
        }
        if let mic = message.bool("mic") { micOpen = mic }
        guard let worn = message.bool("wearing") else { return }
        let before = wearing
        wearing = worn
        if before != worn {
            Log.add("glasses \(worn ? "on" : "off") (mic \(micOpen == true ? "on" : "off"), resume \(policy ?? "?"), mode \(micMode))")
        }
        guard worn, before != true, micOpen != true, micMode != "on_demand" else { return }
        let effectivePolicy = policy ?? BleDiagnostics.currentResumePolicyRaw()
        guard effectivePolicy == "mac" || effectivePolicy == "auto" else { return }
        let source = micMode == "phone" ? "phone" : "glasses"
        Log.add("glasses on under \"\(effectivePolicy)\": asking for \(source) mic")
        link.send(["kind": "audio.control", "open": true, "source": source])
    }
}

/// The conversation page's Continue / Exit (CapabilityMenu.ts sends
/// `input` with containerName "assistant-conversation").
final class ConversationInput: BrainFeature {
    let name = "conversation"
    let uplinkKinds: Set<String> = ["input"]
    private let choice: (String) -> Void

    init(_ choice: @escaping (String) -> Void) { self.choice = choice }

    func pluginConnected() {}

    func handle(kind: String, message: [String: Any]) {
        guard let event = message.object("event"), event.string("containerName") == "assistant-conversation",
              let action = event.string("selectedName") else { return }
        choice(action)
    }
}

// MARK: - Small JSON helpers shared by the features

extension Dictionary where Key == String, Value == Any {
    func string(_ key: String) -> String? { self[key] as? String }
    func int(_ key: String) -> Int? { (self[key] as? NSNumber)?.intValue }
    func bool(_ key: String) -> Bool? { (self[key] as? NSNumber)?.boolValue }
    func object(_ key: String) -> [String: Any]? { self[key] as? [String: Any] }
    func array(_ key: String) -> [Any]? { self[key] as? [Any] }
}
