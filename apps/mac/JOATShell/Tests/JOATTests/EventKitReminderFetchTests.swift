import XCTest
@testable import JOAT

final class EventKitReminderFetchTests: XCTestCase {
    func testReminderDueWindowIsHalfOpenAndExcludesUndatedItems() throws {
        let request = ReminderQueryRequest(
            calendarIdentifiers: ["personal"],
            includeCompleted: false,
            limit: 500,
            dueStartUnixMS: 1_788_220_000_000,
            dueEndUnixMS: 1_788_306_400_000
        )
        let window = try EventKitService.reminderDueWindow(request)

        XCTAssertTrue(EventKitService.reminderIsInDueWindow(1_788_220_000_000, window: window))
        XCTAssertTrue(EventKitService.reminderIsInDueWindow(1_788_306_399_999, window: window))
        XCTAssertFalse(EventKitService.reminderIsInDueWindow(1_788_306_400_000, window: window))
        XCTAssertFalse(EventKitService.reminderIsInDueWindow(nil, window: window))
    }

    func testReminderDueWindowRejectsPartialOrReversedRange() {
        XCTAssertThrowsError(try EventKitService.reminderDueWindow(ReminderQueryRequest(
            calendarIdentifiers: ["personal"],
            includeCompleted: false,
            limit: 5,
            dueStartUnixMS: 10
        )))
        XCTAssertThrowsError(try EventKitService.reminderDueWindow(ReminderQueryRequest(
            calendarIdentifiers: ["personal"],
            includeCompleted: false,
            limit: 5,
            dueStartUnixMS: 20,
            dueEndUnixMS: 20
        )))
    }

    func testTaskCancellationCancelsEventKitRequestAndResumesContinuation() async {
        let started = expectation(description: "EventKit fetch started")
        let cancelled = expectation(description: "EventKit fetch cancelled")
        let identifier = UUID()
        let fetch = CancellableEventKitFetch<Int>(
            cancel: { receivedIdentifier in
                XCTAssertEqual(receivedIdentifier as? UUID, identifier)
                cancelled.fulfill()
            },
            nilResultError: NativeBridgeError(
                "reminder_fetch_unknown",
                "EventKit returned no reminder result"
            )
        )

        let task = Task {
            try await Self.awaitFetch(fetch) { _ in
                started.fulfill()
                return identifier
            }
        }
        await fulfillment(of: [started], timeout: 1)
        task.cancel()

        do {
            _ = try await task.value
            XCTFail("cancelled fetch unexpectedly returned reminders")
        } catch is CancellationError {
            // Expected: EventKit documents that cancellation suppresses its
            // completion callback, so the adapter must resume the waiter.
        } catch {
            XCTFail("unexpected cancellation error: \(error)")
        }
        await fulfillment(of: [cancelled], timeout: 1)
    }

    func testCompletionAfterCancellationCannotResumeContinuationTwice() async {
        let started = expectation(description: "EventKit fetch started")
        let cancelled = expectation(description: "EventKit fetch cancelled")
        let callback = LockedCallback<Int?>()
        let fetch = CancellableEventKitFetch<Int>(
            cancel: { _ in cancelled.fulfill() },
            nilResultError: NativeBridgeError(
                "reminder_fetch_unknown",
                "EventKit returned no reminder result"
            )
        )

        let task = Task {
            try await Self.awaitFetch(fetch) { completion in
                callback.set(completion)
                started.fulfill()
                return UUID()
            }
        }
        await fulfillment(of: [started], timeout: 1)
        task.cancel()
        _ = try? await task.value

        callback.get()?(42)
        await fulfillment(of: [cancelled], timeout: 1)
    }

    func testCancellationBeforeFetchIdentifierReturnsStillCancelsIdentifier() async {
        let started = expectation(description: "EventKit fetch entered start")
        let cancelled = expectation(description: "late EventKit identifier cancelled")
        let releaseIdentifier = DispatchSemaphore(value: 0)
        let identifier = UUID()
        let fetch = CancellableEventKitFetch<Int>(
            cancel: { receivedIdentifier in
                XCTAssertEqual(receivedIdentifier as? UUID, identifier)
                cancelled.fulfill()
            },
            nilResultError: NativeBridgeError(
                "reminder_fetch_unknown",
                "EventKit returned no reminder result"
            )
        )

        let task = Task {
            try await Self.awaitFetch(fetch) { _ in
                started.fulfill()
                releaseIdentifier.wait()
                return identifier
            }
        }
        await fulfillment(of: [started], timeout: 1)
        task.cancel()
        releaseIdentifier.signal()

        do {
            _ = try await task.value
            XCTFail("cancelled fetch unexpectedly returned reminders")
        } catch is CancellationError {
            // Expected.
        } catch {
            XCTFail("unexpected cancellation error: \(error)")
        }
        await fulfillment(of: [cancelled], timeout: 1)
    }

    private static func awaitFetch<Value: Sendable>(
        _ fetch: CancellableEventKitFetch<Value>,
        start: (@escaping (Value?) -> Void) -> Any
    ) async throws -> Value {
        try await withTaskCancellationHandler {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { continuation in
                guard fetch.install(continuation) else {
                    continuation.resume(throwing: CancellationError())
                    return
                }
                let identifier = start { fetch.finish(with: $0) }
                fetch.install(fetchIdentifier: identifier)
            }
        } onCancel: {
            fetch.cancel()
        }
    }
}

private final class LockedCallback<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var callback: ((Value) -> Void)?

    func set(_ newCallback: @escaping (Value) -> Void) {
        lock.withLock { callback = newCallback }
    }

    func get() -> ((Value) -> Void)? {
        lock.withLock { callback }
    }
}
