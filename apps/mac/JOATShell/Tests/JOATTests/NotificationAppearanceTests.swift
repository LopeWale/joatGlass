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
