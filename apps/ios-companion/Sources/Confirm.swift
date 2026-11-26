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
    /// 7-row message/mail review card ceiling (< 280 UTF-16 units in HudPresenter).
    static let messagePromptMaxUnits = 270

    private struct Pending {
        let challengeId: String
        let payloadDigest: String
        let previewDigest: String
        let messageUIActionId: String?
        let expires: Date
        let completion: (Outcome) -> Void
        let timeout: DispatchWorkItem
    }

    private let link: PluginLink
    private let sessionId = ConfirmGate.hex(16)
    private var pending: [String: Pending] = [:]

    var hasPending: Bool { !pending.isEmpty }

    init(link: PluginLink) { self.link = link }

    func pluginConnected() {}

    /// Declines all active confirmation challenges and instructs the glasses
    /// plugin to dismiss the confirmation card immediately.
    @discardableResult
    func declineActive(reason: String) -> Bool {
        guard !pending.isEmpty else { return false }
        let actionIds = Array(pending.keys)
        for actionId in actionIds {
            link.send(["kind": "confirm.cancel", "actionId": actionId])
            Log.add("tx confirm.cancel \(actionId) (\(reason))")
            resolve(actionId, .declined, reason: reason)
        }
        return true
    }

    /// Cancels any pending confirmation bound to `messageUIActionId` (e.g. on
    /// `2x tap: retry` or explicit draft departure) without firing a duplicate
    /// `.declined` completion after the caller already retired the draft.
    @discardableResult
    func cancelPending(forMessageActionId messageUIActionId: String, reason: String) -> Bool {
        let matching = pending.filter { $0.value.messageUIActionId == messageUIActionId }.map(\.key)
        guard !matching.isEmpty else { return false }
        for actionId in matching {
            guard let p = pending.removeValue(forKey: actionId) else { continue }
            p.timeout.cancel()
            link.send(["kind": "confirm.cancel", "actionId": actionId])
            Log.add("tx confirm.cancel \(actionId) (\(reason))")
        }
        return true
    }

    /// Formats `"<cleanHeader>? “<fitBody>”"` guaranteed to stay within `maxUnits`
    /// UTF-16 code units without splitting UTF-16 surrogate pairs.
    static func fitMessagePrompt(header: String, body: String, maxUnits: Int = 270) -> String {
        let cap = max(0, maxUnits)
        guard cap >= 6 else {
            return "\(header)? “\(body)”".utf16Prefix(cap)
        }
        var cleanHeader = header
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: "?: "))
        if cleanHeader.isEmpty { cleanHeader = "Send" }

        let rawBody = body
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
        let cleanBody = rawBody.isEmpty ? "…" : rawBody

        // Wrapper `"? “"` (3 units) + `"”"` (1 unit) = 4 UTF-16 code units.
        let wrapperUnits = 4
        let available = cap - wrapperUnits
        if cleanHeader.utf16.count + cleanBody.utf16.count <= available {
            return "\(cleanHeader)? “\(cleanBody)”"
        }

        // Reserve space for the body first so long headers never starve the preview.
        let minBodyReserve = min(cleanBody.utf16.count, max(1, min(24, available / 2)))
        let headerBudget = max(1, available - minBodyReserve)
        let fitHeader = fitHeaderPreservingPrefix(cleanHeader, units: headerBudget)
        let bodyBudget = max(1, available - fitHeader.utf16.count)
        let fitBody = utf16Fit(cleanBody, units: bodyBudget)
        let combined = "\(fitHeader)? “\(fitBody)”"
        return combined.utf16.count <= cap ? combined : combined.utf16Prefix(cap)
    }

    private static func fitHeaderPreservingPrefix(_ header: String, units: Int) -> String {
        guard header.utf16.count > units else { return header }
        for verb in ["Reply (", "Email ("] where header.hasPrefix(verb) {
            let afterVerb = header.dropFirst(verb.count)
            if let closeRange = afterVerb.range(of: ") to ") {
                let acct = String(afterVerb[..<closeRange.lowerBound])
                let recipient = String(afterVerb[closeRange.upperBound...])
                let fixedOverhead = verb.utf16.count + ") to ".utf16.count
                if units > fixedOverhead + 2 {
                    let innerBudget = units - fixedOverhead
                    let acctBudget = max(1, min(acct.utf16.count, innerBudget / 3))
                    let fitAcct = utf16Fit(acct, units: acctBudget)
                    let recBudget = max(1, innerBudget - fitAcct.utf16.count)
                    let fitRec = utf16Fit(recipient, units: recBudget)
                    let candidate = "\(verb)\(fitAcct)) to \(fitRec)"
                    if candidate.utf16.count <= units { return candidate }
                }
            }
        }
        return utf16Fit(header, units: units)
    }

    private static func utf16Fit(_ text: String, units: Int) -> String {
        guard units > 0 else { return "" }
        if text.utf16.count <= units { return text }
        if units == 1 { return "…" }
        let trimmed = text.utf16Prefix(units - 1).trimmingCharacters(in: .whitespaces)
        return (trimmed.isEmpty ? text.utf16Prefix(units - 1) : trimmed) + "…"
    }

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
                                    previewDigest: previewDigest, messageUIActionId: messageUIActionId,
                                    expires: expires, completion: completion, timeout: timeout)
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
