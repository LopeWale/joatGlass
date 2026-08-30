import Foundation
import Testing
@testable import JOAT

@Test func firstBannerAfterEmptySweepIsNotMistakenForPriming() {
    var tracker = BannerAppearanceTracker()
    #expect(tracker.freshMask(for: []) == [])
    #expect(tracker.freshMask(for: ["first-real-banner"]) == [true])
    #expect(tracker.freshMask(for: ["first-real-banner"]) == [false])
}

@Test func bannerAlreadyPresentOnFirstSweepIsDelivered() {
    var tracker = BannerAppearanceTracker()
    #expect(tracker.freshMask(for: ["already-present"]) == [true])
    #expect(tracker.freshMask(for: ["already-present"]) == [false])
}

@Test func identicalSeparateBannersRemainSeparateAppearanceEdges() {
    var tracker = BannerAppearanceTracker()
    #expect(tracker.freshMask(for: []) == [])
    #expect(tracker.freshMask(for: ["same", "same"]) == [true, true])
    #expect(tracker.freshMask(for: ["same", "same"]) == [false, false])
    #expect(tracker.freshMask(for: []) == [])
    #expect(tracker.freshMask(for: ["same"]) == [true])
}

@Test func sweepStatusContainsOnlyContentFreeObservationCounters() throws {
    let status = NotificationWatcherSweepStatus(
        observedAtUnixMs: 1_788_117_600_000,
        state: .swept,
        sweepCount: 12,
        bannerElementsSeen: 3,
        readableBannersSeen: 2,
        emittedBanners: 1
    )

    let encoded = try JSONEncoder().encode(status)
    let decoded = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]

    #expect(decoded?["schema"] as? String == "joat.notification-watcher-status.v1")
    #expect(decoded?["state"] as? String == "swept")
    #expect(decoded?["sweep_count"] as? Int == 12)
    #expect(decoded?["banner_elements_seen"] as? Int == 3)
    #expect(decoded?["readable_banners_seen"] as? Int == 2)
    #expect(decoded?["emitted_banners"] as? Int == 1)
    #expect(decoded?["title"] == nil)
    #expect(decoded?["body"] == nil)
    #expect(decoded?["app"] == nil)
}
