import Foundation
import CoreLocation
#if canImport(UIKit)
import UIKit
#endif

/// Native iPhone CoreLocation source for JOAT Brain: WhenInUse authorization,
/// fast one-shot proximity/origin fixes, and an active-route-only 1 Hz GPS
/// stream backed by `CLBackgroundActivitySession` + the iOS blue navigation
/// indicator while turn-by-turn directions are running.
@MainActor
final class PhoneLocation: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = PhoneLocation()

    struct Fix: Equatable {
        let point: NavPoint
        let accuracy: Double?
        let heading: Double?
        let speed: Double?
        let at: Date
    }

    private let manager = CLLocationManager()
    @Published private(set) var authorization: CLAuthorizationStatus
    @Published private(set) var latest: Fix?

    private var permissionWaiters: [UUID: CheckedContinuation<CLAuthorizationStatus, Never>] = [:]
    private var oneShotWaiters: [UUID: CheckedContinuation<NavPoint?, Never>] = [:]
    private var navigationFixHandler: ((Fix) -> Void)?
    private(set) var isStreamingNavigation = false

    #if os(iOS)
    private var backgroundSession: CLBackgroundActivitySession?
    #endif

    var isAuthorized: Bool {
        #if os(iOS)
        return authorization == .authorizedWhenInUse || authorization == .authorizedAlways
        #else
        return authorization == .authorizedAlways
        #endif
    }

    override init() {
        authorization = manager.authorizationStatus
        super.init()
        manager.delegate = self
        if isAuthorized,
           let loc = manager.location,
           let seeded = Self.makeFix(from: loc),
           abs(seeded.at.timeIntervalSinceNow) <= 3600 {
            latest = seeded
        }
    }

    func requestPermission() async -> CLAuthorizationStatus {
        authorization = manager.authorizationStatus
        guard authorization == .notDetermined else { return authorization }

        let id = UUID()
        return await withCheckedContinuation { continuation in
            permissionWaiters[id] = continuation
            #if os(iOS)
            manager.requestWhenInUseAuthorization()
            #else
            manager.requestAlwaysAuthorization()
            #endif
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard let self, let waiter = self.permissionWaiters.removeValue(forKey: id) else { return }
                self.authorization = self.manager.authorizationStatus
                waiter.resume(returning: self.authorization)
            }
        }
    }

    func requestOneShot(timeout: TimeInterval = 2.5) async -> NavPoint? {
        authorization = manager.authorizationStatus
        #if os(iOS)
        if authorization == .notDetermined, UIApplication.shared.applicationState == .active {
            _ = await requestPermission()
        }
        #endif
        guard isAuthorized else { return nil }

        let now = Date()
        if let latest, now.timeIntervalSince(latest.at) <= 15, (latest.accuracy ?? 100) <= 65 {
            return latest.point
        }
        if let loc = manager.location,
           let cached = Self.makeFix(from: loc),
           now.timeIntervalSince(cached.at) <= 15,
           (cached.accuracy ?? 100) <= 65 {
            latest = cached
            return cached.point
        }

        let id = UUID()
        let waitNs = UInt64(max(0.1, timeout) * 1_000_000_000)
        return await withCheckedContinuation { continuation in
            oneShotWaiters[id] = continuation
            if !isStreamingNavigation {
                manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
                manager.distanceFilter = kCLDistanceFilterNone
                manager.startUpdatingLocation()
            }
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: waitNs)
                guard let self, let waiter = self.oneShotWaiters.removeValue(forKey: id) else { return }
                if !self.isStreamingNavigation, self.oneShotWaiters.isEmpty {
                    self.manager.stopUpdatingLocation()
                }
                if let latest = self.latest, Date().timeIntervalSince(latest.at) <= 120 {
                    waiter.resume(returning: latest.point)
                } else if let loc = self.manager.location,
                          let fallback = Self.makeFix(from: loc),
                          Date().timeIntervalSince(fallback.at) <= 120 {
                    self.latest = fallback
                    waiter.resume(returning: fallback.point)
                } else {
                    waiter.resume(returning: nil)
                }
            }
        }
    }

    func startNavigationUpdates(mode: String, onFix: @escaping (Fix) -> Void) {
        authorization = manager.authorizationStatus
        guard isAuthorized else {
            Log.add("phone location stream skipped: not authorized (\(authorization.rawValue))")
            return
        }
        let driving = mode == "driving"
        isStreamingNavigation = true
        navigationFixHandler = onFix
        manager.desiredAccuracy = driving ? kCLLocationAccuracyBestForNavigation : kCLLocationAccuracyBest
        manager.distanceFilter = kCLDistanceFilterNone
        manager.activityType = driving ? .automotiveNavigation : .fitness
        #if os(iOS)
        manager.pausesLocationUpdatesAutomatically = false
        manager.allowsBackgroundLocationUpdates = true
        manager.showsBackgroundLocationIndicator = true
        if backgroundSession == nil {
            backgroundSession = CLBackgroundActivitySession()
        }
        #endif
        manager.startUpdatingLocation()
        Log.add("phone location navigation stream started (mode=\(mode))")
    }

    func stopNavigationUpdates() {
        let wasStreaming = isStreamingNavigation
        isStreamingNavigation = false
        navigationFixHandler = nil
        if oneShotWaiters.isEmpty {
            manager.stopUpdatingLocation()
        }
        #if os(iOS)
        manager.allowsBackgroundLocationUpdates = false
        manager.showsBackgroundLocationIndicator = false
        backgroundSession?.invalidate()
        backgroundSession = nil
        #endif
        if wasStreaming {
            Log.add("phone location navigation stream stopped")
        }
    }

    static func makeFix(from loc: CLLocation) -> Fix? {
        guard loc.horizontalAccuracy >= 0 else { return nil }
        let point = NavPoint(loc.coordinate)
        guard point.isValid else { return nil }
        let accuracy = loc.horizontalAccuracy.isFinite ? loc.horizontalAccuracy : nil
        let heading = (loc.course >= 0 && loc.course.isFinite) ? loc.course : nil
        let speed = (loc.speed >= 0 && loc.speed.isFinite) ? loc.speed : nil
        return Fix(point: point, accuracy: accuracy, heading: heading, speed: speed, at: loc.timestamp)
    }

    // MARK: - CLLocationManagerDelegate

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.authorization = status
            Log.add("phone location authorization=\(status.rawValue)")
            if status != .notDetermined, !self.permissionWaiters.isEmpty {
                let waiters = self.permissionWaiters.values
                self.permissionWaiters.removeAll()
                for waiter in waiters { waiter.resume(returning: status) }
            }
            if !self.isAuthorized, self.isStreamingNavigation {
                self.stopNavigationUpdates()
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let freshest = locations.last else { return }
        Task { @MainActor [weak self] in
            guard let self, let fix = Self.makeFix(from: freshest) else { return }
            // Ignore stale cached replay older than 30 s when streaming live navigation.
            let age = Date().timeIntervalSince(fix.at)
            if age <= 120 {
                self.latest = fix
            }
            if !self.oneShotWaiters.isEmpty, age <= 60 {
                let waiters = self.oneShotWaiters.values
                self.oneShotWaiters.removeAll()
                if !self.isStreamingNavigation {
                    self.manager.stopUpdatingLocation()
                }
                for waiter in waiters { waiter.resume(returning: fix.point) }
            }
            if self.isStreamingNavigation, age <= 15 {
                self.navigationFixHandler?(fix)
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        let ns = error as NSError
        // kCLErrorLocationUnknown (0) is transient while GPS warms up; keep waiting.
        if ns.domain == kCLErrorDomain, ns.code == CLError.locationUnknown.rawValue {
            return
        }
        Task { @MainActor [weak self] in
            guard let self else { return }
            Log.add("phone location error: \(ns.domain) \(ns.code)")
            if !self.oneShotWaiters.isEmpty {
                let waiters = self.oneShotWaiters.values
                self.oneShotWaiters.removeAll()
                if !self.isStreamingNavigation {
                    self.manager.stopUpdatingLocation()
                }
                let fallback = self.latest.flatMap { Date().timeIntervalSince($0.at) <= 120 ? $0.point : nil }
                for waiter in waiters { waiter.resume(returning: fallback) }
            }
        }
    }
}
