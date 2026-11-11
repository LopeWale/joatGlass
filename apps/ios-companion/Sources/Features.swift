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
        DispatchQueue.main.async {
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
    }
}

/// The mic after the glasses come back on (__main__.py on_device). The
/// plugin stops the glasses mic when they come off (DeviceMonitor.ts) and,
/// when they go back on, follows the wearer's "After an interruption"
/// choice on its phone page: "Resume automatically" restarts it, "Ask
/// first" asks on the lens, "Let Mac decide" waits for the brain — which on
/// the phone is this. It always wants ears, so it asks for the mic.
final class MicResume: BrainFeature {
    let name = "mic resume"
    let uplinkKinds: Set<String> = ["device", "policy"]
    private let link: PluginLink
    private var policy: String?
    private var micOpen: Bool?
    private var wearing: Bool?

    init(link: PluginLink) { self.link = link }

    func pluginConnected() {
        micOpen = nil
        wearing = nil
    }

    func handle(kind: String, message: [String: Any]) {
        if kind == "policy" {
            policy = message.string("resume")
            Log.add("mic resume policy: \(policy ?? "?")")
            return
        }
        if let mic = message.bool("mic") { micOpen = mic }
        guard let worn = message.bool("wearing") else { return }
        let before = wearing
        wearing = worn
        if before != worn { Log.add("glasses \(worn ? "on" : "off") (mic \(micOpen == true ? "on" : "off"), resume \(policy ?? "?"))") }
        guard worn, before != true, micOpen != true, policy == "mac" else { return }
        Log.add("glasses on under \"Let Mac decide\": asking for the mic")
        link.send(["kind": "audio.control", "open": true, "source": "glasses"])
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
