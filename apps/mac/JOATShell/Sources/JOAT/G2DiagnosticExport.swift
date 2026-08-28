import Foundation

/// The shell owns the user-facing export boundary. It revalidates every field
/// instead of copying the on-disk receipt verbatim, so a support archive never
/// grows a transcript, token, device identifier, or arbitrary extension field.
enum G2DiagnosticExport {
    static let sourceURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/JOAT/g2-diagnostics.json")

    enum ExportError: LocalizedError {
        case unavailable
        case malformed

        var errorDescription: String? {
            switch self {
            case .unavailable: "No G2 diagnostic trace is available to export."
            case .malformed: "The G2 diagnostic trace was not a valid redacted receipt."
            }
        }
    }

    static func clear() throws {
        guard FileManager.default.fileExists(atPath: sourceURL.path) else { return }
        try FileManager.default.removeItem(at: sourceURL)
    }

    static func export(to destination: URL, nowMs: Int64 = Int64(Date().timeIntervalSince1970 * 1_000)) throws {
        let data: Data
        do {
            data = try Data(contentsOf: sourceURL)
        } catch {
            throw ExportError.unavailable
        }
        let archive = try redactedArchive(from: data, exportedAtMs: nowMs)
        try archive.write(to: destination, options: [.atomic])
    }

    static func redactedArchive(from data: Data, exportedAtMs: Int64) throws -> Data {
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              root["schema"] as? String == "joat.g2-diagnostic.v1",
              let rawEvents = root["events"] as? [[String: Any]]
        else { throw ExportError.malformed }

        let events = rawEvents.compactMap(redactedEvent)
        guard events.count == rawEvents.count else { throw ExportError.malformed }
        return try JSONSerialization.data(
            withJSONObject: [
                "schema": "joat.g2-diagnostic-export.v1",
                "exportedAtMs": exportedAtMs,
                "events": events,
            ],
            options: [.sortedKeys]
        )
    }

    private static func redactedEvent(_ event: [String: Any]) -> [String: Any]? {
        guard let sequence = positiveInt(event["sequence"]),
              let recordedAtMs = positiveInt(event["recordedAtMs"]),
              let type = event["type"] as? String
        else { return nil }
        var output: [String: Any] = [
            "sequence": sequence,
            "recordedAtMs": recordedAtMs,
            "type": type,
        ]
        switch type {
        case "page_transition":
            guard let from = page(event["from"]), let to = page(event["to"]),
                  let reason = one(of: event["reason"], ["show_sidebar", "show_list", "show_overview", "show_confirmation", "dismiss_confirmation"]),
                  let outcome = one(of: event["outcome"], ["success", "failed"])
            else { return nil }
            output["from"] = from; output["to"] = to
            output["reason"] = reason; output["outcome"] = outcome
        case "gesture":
            guard let gesture = one(of: event["gesture"], ["tap", "double_press", "long_press", "long_press_release", "scroll_up", "scroll_down", "menu_item", "foreground_enter", "foreground_exit", "system_exit", "abnormal_exit", "imu_report", "unknown"]),
                  let delivery = one(of: event["delivery"], ["sys", "text", "list", "menu"])
            else { return nil }
            output["gesture"] = gesture; output["delivery"] = delivery
        case "tile_upload":
            guard let frame = positiveInt(event["frame"]),
                  let order = event["order"] as? Int, (0...3).contains(order),
                  let containerId = event["containerId"] as? Int, [4, 8, 9, 10].contains(containerId),
                  let outcome = one(of: event["outcome"], ["success", "failed", "error"])
            else { return nil }
            output["frame"] = frame; output["order"] = order
            output["containerId"] = containerId; output["outcome"] = outcome
        case "self_heal":
            guard let surface = page(event["surface"]),
                  let target = one(of: event["target"], ["text", "visual"]),
                  let decision = one(of: event["decision"], ["attempt", "recovered", "abandoned", "skipped"]),
                  let containerId = event["containerId"] as? Int, (1...10).contains(containerId)
            else { return nil }
            output["surface"] = surface; output["target"] = target
            output["decision"] = decision; output["containerId"] = containerId
        default:
            return nil
        }
        return output
    }

    private static func page(_ value: Any?) -> String? {
        one(of: value, ["overview", "sidebar", "list", "confirmation"])
    }

    private static func one(of value: Any?, _ allowed: [String]) -> String? {
        guard let string = value as? String, allowed.contains(string) else { return nil }
        return string
    }

    private static func positiveInt(_ value: Any?) -> Int? {
        guard let value = value as? Int, value > 0 else { return nil }
        return value
    }
}
