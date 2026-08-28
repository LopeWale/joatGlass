import Foundation

actor BridgeRouter {
    private let eventKit: EventKitService

    init(eventKit: EventKitService = EventKitService()) {
        self.eventKit = eventKit
    }

    func start() async {
        await eventKit.startObservingChanges()
    }

    func authorizationSnapshot() async -> EventKitAuthorizationSnapshot {
        await eventKit.authorizationSnapshot()
    }

    func requestFullAccessFromMacUI(
        entity: String
    ) async throws -> EventKitAuthorizationSnapshot {
        try await eventKit.requestFullAccess(entity: entity)
    }

    func handle(_ request: BridgeRequest) async -> BridgeResponse {
        do {
            guard request.version == BridgeContract.version else {
                throw NativeBridgeError("version_mismatch", "unsupported bridge protocol version")
            }
            guard !request.requestID.isEmpty else {
                throw NativeBridgeError("invalid_request_id", "request ID is required")
            }
            let nowMS = Int64((Date().timeIntervalSince1970 * 1_000).rounded())
            guard request.deadlineUnixMS >= nowMS else {
                throw NativeBridgeError("deadline_exceeded", "request deadline has passed")
            }

            let result: JSONValue
            let commitPhase: String
            switch request.method {
            case "capabilities":
                result = try await BridgeContract.encode(eventKit.capabilities())
                commitPhase = "read_only"
            case "authorization.status":
                result = try await BridgeContract.encode(eventKit.authorizationSnapshot())
                commitPhase = "read_only"
            case "authorization.requestFullAccess":
                // IPC input cannot prove that a permission prompt was initiated
                // from the foreground Mac UI. Keep TCC prompting behind
                // requestFullAccessFromMacUI(entity:) only.
                throw NativeBridgeError(
                    "permission_request_ui_only",
                    "EventKit access can only be requested from the Mac app UI"
                )
            case "localTime.resolve":
                let body = try BridgeContract.decode(LocalTimeResolveRequest.self, from: request.body)
                result = try BridgeContract.encode(LocalTimeResolver.resolve(body.civilTime))
                commitPhase = "read_only"
            case "calendar.listContainers":
                result = try await BridgeContract.encode(eventKit.listContainers(entity: "calendar"))
                commitPhase = "read_only"
            case "reminders.listContainers":
                result = try await BridgeContract.encode(eventKit.listContainers(entity: "reminders"))
                commitPhase = "read_only"
            case "reminders.query":
                let body = try BridgeContract.decode(ReminderQueryRequest.self, from: request.body)
                result = try await BridgeContract.encode(eventKit.queryReminders(body))
                commitPhase = "read_only"
            case "calendar.query":
                let body = try BridgeContract.decode(CalendarQueryRequest.self, from: request.body)
                result = try await BridgeContract.encode(eventKit.queryEvents(body))
                commitPhase = "read_only"
            case "calendar.create":
                let binding = try writeBinding(request)
                let body = try BridgeContract.decode(CalendarCreateRequest.self, from: request.body)
                result = try await eventKit.createEvent(
                    body,
                    attemptID: binding.attemptID,
                    payloadDigest: binding.payloadDigest
                )
                commitPhase = "committed_readback"
            case "reminders.create":
                let binding = try writeBinding(request)
                let body = try BridgeContract.decode(ReminderCreateRequest.self, from: request.body)
                result = try await eventKit.createReminder(
                    body,
                    attemptID: binding.attemptID,
                    payloadDigest: binding.payloadDigest
                )
                commitPhase = "committed_readback"
            case "reminders.complete":
                let binding = try writeBinding(request)
                let body = try BridgeContract.decode(ReminderCompleteRequest.self, from: request.body)
                result = try await eventKit.completeReminder(
                    body,
                    attemptID: binding.attemptID,
                    payloadDigest: binding.payloadDigest
                )
                commitPhase = "committed_readback"
            default:
                throw NativeBridgeError("unknown_method", "bridge method is not supported")
            }
            return await response(
                requestID: request.requestID,
                commitPhase: commitPhase,
                result: result,
                error: nil
            )
        } catch let error as NativeBridgeError {
            return await response(
                requestID: request.requestID,
                commitPhase: error.commitPhase,
                result: nil,
                error: BridgeTypedError(
                    code: error.code,
                    message: error.message,
                    retryable: error.retryable
                )
            )
        } catch {
            return await response(
                requestID: request.requestID,
                commitPhase: "none",
                result: nil,
                error: BridgeTypedError(
                    code: "invalid_request",
                    message: "request body does not match the typed bridge contract",
                    retryable: false
                )
            )
        }
    }

    private func writeBinding(_ request: BridgeRequest) throws -> (
        attemptID: String,
        payloadDigest: String
    ) {
        guard let attemptID = request.attemptID, !attemptID.isEmpty,
              let digest = request.payloadDigest,
              digest.count == 64,
              digest.allSatisfy({ $0.isHexDigit })
        else {
            throw NativeBridgeError(
                "missing_write_binding",
                "writes require an attempt ID and SHA-256 payload digest"
            )
        }
        return (attemptID, digest.lowercased())
    }

    private func response(
        requestID: String,
        commitPhase: String,
        result: JSONValue?,
        error: BridgeTypedError?
    ) async -> BridgeResponse {
        BridgeResponse(
            version: BridgeContract.version,
            requestID: requestID,
            storeInstanceID: eventKit.storeInstanceID,
            storeGeneration: await eventKit.storeGeneration,
            authorization: await eventKit.authorizationSnapshot(),
            commitPhase: commitPhase,
            result: result,
            error: error
        )
    }
}
