import Foundation
import CryptoKit

/// The glasses' yes/no gate before any native write: a port of the Mac's
/// HubGatewayTransport.request_confirm (ble/hub_gateway.py) and the
/// `confirm.request` / `confirm` wire (core/protocol.py ConfirmRequest).
/// The plugin echoes every binding; anything that does not match, or comes
/// after the window, is ignored and the write does not happen.
final class ConfirmGate: BrainFeature {
    enum Outcome { case accepted, declined, unverified }

    let name = "confirm"
    let uplinkKinds: Set<String> = ["confirm"]
    /// productivity_actions.py _CONFIRM_SECONDS
    static let windowSeconds: Double = 20
    /// protocol.py CONFIRM_PROMPT_MAX_UTF16_UNITS
    static let promptMaxUnits = 120

    private struct Pending {
        let challengeId: String
        let payloadDigest: String
        let previewDigest: String
        let expires: Date
        let completion: (Outcome) -> Void
        let timeout: DispatchWorkItem
    }

    private let link: PluginLink
    private let sessionId = ConfirmGate.hex(16)
    private var pending: [String: Pending] = [:]

    init(link: PluginLink) { self.link = link }

    func pluginConnected() {}

    /// Ask on the glasses; `completion` runs exactly once on the main queue.
    /// `messageUIActionId`: the Messages draft this confirms (the plugin
    /// draws it over the conversation and drops it when the draft ends).
    func ask(prompt: String, payload: String, diagnosticKind: String? = nil, messageUIActionId: String? = nil,
             completion: @escaping (Outcome) -> Void) {
        let actionId = "phone-\(Self.hex(8))"
        let challengeId = Self.hex(16)
        let payloadDigest = Self.sha256(payload)
        let previewDigest = Self.sha256(prompt)
        let expires = Date().addingTimeInterval(Self.windowSeconds)
        let timeout = DispatchWorkItem { [weak self] in self?.resolve(actionId, .unverified, reason: "no answer in time") }
        pending[actionId] = Pending(challengeId: challengeId, payloadDigest: payloadDigest,
                                    previewDigest: previewDigest, expires: expires,
                                    completion: completion, timeout: timeout)
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.windowSeconds + 1, execute: timeout)
        var request: [String: Any] = [
            "kind": "confirm.request", "actionId": actionId, "prompt": prompt,
            "challengeId": challengeId, "sessionId": sessionId,
            "payloadDigest": payloadDigest, "previewDigest": previewDigest,
            "expiresAtMs": Int(expires.timeIntervalSince1970 * 1000),
        ]
        if let diagnosticKind { request["diagnosticKind"] = diagnosticKind }
        if let messageUIActionId { request["messageUIActionId"] = messageUIActionId }
        link.send(request)
        Log.add("tx confirm.request \(actionId): \(prompt)")
    }

    func handle(kind: String, message: [String: Any]) {
        guard let actionId = message.string("actionId"), let p = pending[actionId] else { return }
        let matches = message.string("challengeId") == p.challengeId
            && message.string("sessionId") == sessionId
            && message.string("payloadDigest") == p.payloadDigest
            && message.string("previewDigest") == p.previewDigest
            && Date() <= p.expires
        guard matches else {
            Log.add("confirm \(actionId) ignored: binding mismatch or expired")
            return
        }
        resolve(actionId, message.bool("accepted") == true ? .accepted : .declined, reason: "wearer")
    }

    private func resolve(_ actionId: String, _ outcome: Outcome, reason: String) {
        guard let p = pending.removeValue(forKey: actionId) else { return }
        p.timeout.cancel()
        Log.add("confirm \(actionId) → \(outcome) (\(reason))")
        p.completion(outcome)
    }

    static func hex(_ bytes: Int) -> String {
        (0..<bytes).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
    }

    static func sha256(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
