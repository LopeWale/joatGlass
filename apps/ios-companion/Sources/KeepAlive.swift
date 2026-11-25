import Foundation
import AVFoundation

/// What keeps JOAT Brain running behind the Even app and with the screen off:
/// the `audio` background mode, held by an active session playing inaudible
/// silence, mixed with other apps' audio.
///
/// Earlier builds held a record session on the phone's own mic. Switching
/// to the Even app or locking the screen changed the audio route, stopping
/// the engine (or restarting it with a tap in the old input format, throwing
/// an exception), which let iOS suspend the app so the glasses could not
/// reconnect. JOAT never used that mic (the glasses' audio arrives over the
/// socket), so the keep-alive no longer touches it: playback only, rebuilt
/// after any interruption, route or configuration change, and checked every
/// 15 s.
final class KeepAlive: ObservableObject {
    @Published private(set) var active = false
    @Published private(set) var route = "-"
    @Published private(set) var mode = "-"

    private var engine: AVAudioEngine?
    private var observers: [NSObjectProtocol] = []
    private var watchdog: Timer?
    /// Switched on by the wearer (Start) or at launch; Stop switches it off.
    private var wanted = false

    func start() {
        wanted = true
        observe()
        if watchdog == nil {
            watchdog = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.check() }
        }
        guard !active else { return }
        if startSilence() {
            active = true
            mode = "silence"
            describeRoute()
            Log.add("keep-alive on (silence)")
        }
    }

    func stop() {
        wanted = false
        watchdog?.invalidate()
        watchdog = nil
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        observers.removeAll()
        engine?.stop()
        engine = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        active = false
        mode = "-"
        Log.add("keep-alive off")
    }

    /// The engine stopped (an interruption, a route or configuration change)
    /// while it should run: build it again.
    func check() {
        guard wanted else { return }
        if engine?.isRunning == true { return }
        Log.add("keep-alive engine not running; restarting")
        restart()
    }

    private func restart() {
        engine?.stop()
        engine = nil
        active = startSilence()
        mode = active ? "silence" : "-"
        if !active { Log.add("keep-alive restart failed; next try in 15 s") }
    }

    private func startSilence() -> Bool {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try session.setActive(true)
            let e = AVAudioEngine()
            let silence = AVAudioSourceNode { _, _, _, buffers in
                for b in UnsafeMutableAudioBufferListPointer(buffers) {
                    if let data = b.mData { memset(data, 0, Int(b.mDataByteSize)) }
                }
                return noErr
            }
            e.attach(silence)
            e.connect(silence, to: e.mainMixerNode, format: nil)
            try e.start()
            engine = e
            return true
        } catch {
            Log.add("keep-alive failed: \(error)")
            return false
        }
    }

    private func observe() {
        guard observers.isEmpty else { return }
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            switch type {
            case .began:
                Log.add("audio interruption began (call/alarm?)")
                self?.active = false
                self?.mode = "interrupted"
            case .ended:
                Log.add("audio interruption ended; rebuilding keep-alive")
                self?.restart()
            @unknown default:
                break
            }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] note in
            let reasonCode = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt)
                .flatMap(AVAudioSession.RouteChangeReason.init(rawValue:))
            self?.describeRoute(reason: reasonCode)
            self?.check()
        })
        observers.append(nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] note in
            guard let self, let changed = note.object as? AVAudioEngine, changed === self.engine else { return }
            Log.add("audio configuration changed; rebuilding keep-alive")
            self.restart()
        })
        observers.append(nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            Log.add("media services reset; rebuilding keep-alive")
            self?.restart()
        })
    }

    private func describeRoute(reason: AVAudioSession.RouteChangeReason? = nil) {
        let r = AVAudioSession.sharedInstance().currentRoute
        let ins = r.inputs.map { "\($0.portType.rawValue)" }.joined(separator: ",")
        let outs = r.outputs.map { "\($0.portType.rawValue)" }.joined(separator: ",")
        let btPorts: Set<AVAudioSession.Port> = [.bluetoothA2DP, .bluetoothHFP, .bluetoothLE, .carAudio]
        let hasBT = r.outputs.contains { btPorts.contains($0.portType) }
        let reasonLabel: String? = reason.map {
            switch $0 {
            case .newDeviceAvailable: return "newDevice"
            case .oldDeviceUnavailable: return "deviceLeft"
            case .categoryChange: return "category"
            case .override: return "override"
            case .wakeFromSleep: return "wake"
            case .noSuitableRouteForCategory: return "noRoute"
            case .routeConfigurationChange: return "config"
            case .unknown: return "unknown"
            @unknown default: return "other"
            }
        }
        route = "in[\(ins)] out[\(outs)]"
        if let reasonLabel {
            Log.add("route \(route) (reason=\(reasonLabel), btOut=\(hasBT))")
        } else {
            Log.add("route \(route)")
        }
        Task { @MainActor in
            BleDiagnostics.shared.recordAudioRoute(route: self.route, reason: reasonLabel, bluetoothActive: hasBT)
        }
    }
}
