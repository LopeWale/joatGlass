import Foundation

/// Makes a deliberate boundary around legacy Objective-C values that Apple
/// has not annotated Sendable. The caller must ensure the wrapped value has a
/// single logical owner after the transfer.
struct UncheckedEventKitTransfer<Value>: @unchecked Sendable {
    let value: Value
}

/// Owns the continuation and opaque identifier for one EventKit callback
/// fetch. Starting and cancelling the EKEventStore operation remain the
/// responsibility of its owning actor.
final class CancellableEventKitFetch<Value: Sendable>: @unchecked Sendable {
    typealias Cancel = @Sendable (Any) -> Void

    private let cancelRequest: Cancel
    private let nilResultError: NativeBridgeError

    private let stateLock = NSLock()
    private var continuation: CheckedContinuation<Value, Error>?
    private var fetchIdentifier: Any?
    private var cancellationRequested = false
    private var isFinished = false

    init(
        cancel: @escaping Cancel,
        nilResultError: NativeBridgeError
    ) {
        self.cancelRequest = cancel
        self.nilResultError = nilResultError
    }

    func install(_ newContinuation: CheckedContinuation<Value, Error>) -> Bool {
        stateLock.withLock {
            guard !cancellationRequested, !isFinished, continuation == nil else {
                return false
            }
            continuation = newContinuation
            return true
        }
    }

    func install(fetchIdentifier identifier: Any) {
        let shouldCancel = stateLock.withLock { () -> Bool in
            if cancellationRequested { return true }
            guard !isFinished else { return false }
            fetchIdentifier = identifier
            return false
        }
        if shouldCancel { cancelRequest(identifier) }
    }

    func finish(with value: Value?) {
        let installedContinuation = stateLock.withLock { () -> CheckedContinuation<Value, Error>? in
            guard !isFinished else { return nil }
            isFinished = true
            let installed = continuation
            continuation = nil
            fetchIdentifier = nil
            return installed
        }
        guard let installedContinuation else { return }
        if let value {
            installedContinuation.resume(returning: value)
        } else {
            installedContinuation.resume(throwing: nilResultError)
        }
    }

    func cancel() {
        let cancellation = stateLock.withLock { () -> (
            continuation: CheckedContinuation<Value, Error>?,
            identifier: Any?
        )? in
            guard !isFinished else { return nil }
            cancellationRequested = true
            isFinished = true
            let result = (continuation, fetchIdentifier)
            continuation = nil
            fetchIdentifier = nil
            return result
        }
        guard let cancellation else { return }
        cancellation.continuation?.resume(throwing: CancellationError())
        if let identifier = cancellation.identifier { cancelRequest(identifier) }
    }
}
