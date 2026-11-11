import Foundation
import UIKit

/// Spike 2: does the brain stay alive, and does the glasses link hold, with
/// the phone locked for an hour? One line every 30 s, to the log file, from
/// the Brain's own timer — so "Brain alive" is measured independently of the
/// plugin (a Private build may lose its WebView on lock; Even's private-
/// testing doc says so).
final class Endurance: ObservableObject {
    @Published private(set) var summary = "—"
    private let server: LoopbackServer
    private let started = Date()
    private var timer: Timer?
    private var startBattery: Float = -1
    private var ticks = 0

    init(server: LoopbackServer) { self.server = server }

    func start() {
        UIDevice.current.isBatteryMonitoringEnabled = true
        startBattery = UIDevice.current.batteryLevel
        Log.add("endurance start battery=\(Self.pct(startBattery)) state=\(Self.batteryState())")
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.tick() }
    }

    private func tick() {
        ticks += 1
        let up = Int(Date().timeIntervalSince(started))
        let sincePong = server.lastPongAt.map { "\(Int(Date().timeIntervalSince($0)))s" } ?? "never"
        let battery = UIDevice.current.batteryLevel
        let line = "tick #\(ticks) up=\(up / 60)m\(up % 60)s app=\(Self.appState()) "
            + "plugin=\(server.clients > 0 && server.authenticated ? "linked" : "none") "
            + "sessions=\(server.pluginSessions) pongs=\(server.pongs) lastPong=\(sincePong) "
            + "maxGap=\(server.maxPongGapMs)ms audioFrames=\(server.audioFrames) "
            + "battery=\(Self.pct(battery)) (\(Self.batteryState())) thermal=\(Self.thermal()) mem=\(Self.footprintMB())MB"
        Log.add(line)
        DispatchQueue.main.async { self.summary = line }
    }

    private static func pct(_ level: Float) -> String { level < 0 ? "?" : "\(Int((level * 100).rounded()))%" }

    private static func batteryState() -> String {
        switch UIDevice.current.batteryState {
        case .charging: return "charging"
        case .full: return "full"
        case .unplugged: return "unplugged"
        default: return "unknown"
        }
    }

    private static func appState() -> String {
        switch UIApplication.shared.applicationState {
        case .active: return "active"
        case .inactive: return "inactive"
        case .background: return "background"
        @unknown default: return "?"
        }
    }

    private static func thermal() -> String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "?"
        }
    }

    /// The app's physical memory footprint, the figure iOS terminates on.
    private static func footprintMB() -> Int {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        return kr == KERN_SUCCESS ? Int(info.phys_footprint / 1_048_576) : -1
    }
}
