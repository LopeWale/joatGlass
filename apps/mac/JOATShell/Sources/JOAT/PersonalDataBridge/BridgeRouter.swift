import CryptoKit
import Foundation

actor BridgeRouter {
    private let eventKit: EventKitService
    private let openClawCredentials: any OpenClawCredentialServicing
    private let a2aCredentials: any A2ATokenServicing

    init(
        eventKit: EventKitService = EventKitService(),
        openClawCredentials: any OpenClawCredentialServicing = OpenClawCredentialService(),
        a2aCredentials: any A2ATokenServicing = A2ACredentialService()
    ) {
        self.eventKit = eventKit
        self.openClawCredentials = openClawCredentials
        self.a2aCredentials = a2aCredentials
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
            case "reminders.get":
                let body = try BridgeContract.decode(ReminderGetRequest.self, from: request.body)
                result = try await BridgeContract.encode(eventKit.getReminder(body))
                commitPhase = "read_only"
            case "reminders.query":
                let body = try BridgeContract.decode(ReminderQueryRequest.self, from: request.body)
                result = try await BridgeContract.encode(eventKit.queryReminders(body))
                commitPhase = "read_only"
            case "calendar.query":
                let body = try BridgeContract.decode(CalendarQueryRequest.self, from: request.body)
                result = try await BridgeContract.encode(eventKit.queryEvents(body))
                commitPhase = "read_only"
            case "openclaw.identity.get":
                let body = try BridgeContract.decode(OpenClawProfileRequest.self, from: request.body)
                result = try await BridgeContract.encode(
                    openClawCredentials.identity(profileID: body.profileID)
                )
                commitPhase = "local_secret_created_or_loaded"
            case "openclaw.identity.sign":
                let body = try BridgeContract.decode(OpenClawSignRequest.self, from: request.body)
                result = try await BridgeContract.encode(OpenClawSignatureDTO(
                    signature: openClawCredentials.sign(
                        profileID: body.profileID,
                        payload: body.payload
                    )
                ))
                commitPhase = "local_secret_operation"
            case "a2a.token.load":
                let body = try BridgeContract.decode(A2ATokenLoadRequest.self, from: request.body)
                if let token = try a2aCredentials.loadToken(profileID: body.profileID, endpoint: body.endpoint) {
                    result = try BridgeContract.encode(A2ATokenDTO(token: token))
                } else {
                    result = .null
                }
                commitPhase = "local_secret_read"
            case "openclaw.token.load":
                let body = try BridgeContract.decode(OpenClawTokenRequest.self, from: request.body)
                result = try await BridgeContract.encode(openClawCredentials.loadToken(
                    profileID: body.profileID,
                    deviceID: body.deviceID,
                    role: body.role
                ))
                commitPhase = "local_secret_read"
            case "openclaw.token.store":
                let body = try BridgeContract.decode(
                    OpenClawTokenStoreRequest.self,
                    from: request.body
                )
                try await openClawCredentials.storeToken(
                    profileID: body.profileID,
                    deviceID: body.deviceID,
                    role: body.role,
                    token: body.token,
                    scopes: body.scopes
                )
                result = try BridgeContract.encode(OpenClawStoredDTO(stored: true))
                commitPhase = "local_secret_stored"
            case "calendar.create":
                let body = try BridgeContract.decode(CalendarCreateRequest.self, from: request.body)
                let binding = try writeBinding(request)
                result = try await eventKit.createEvent(
                    body,
                    attemptID: binding.attemptID,
                    payloadDigest: binding.payloadDigest
                )
                commitPhase = "committed_readback"
            case "reminders.create":
                let body = try BridgeContract.decode(ReminderCreateRequest.self, from: request.body)
                let binding = try writeBinding(request)
                result = try await eventKit.createReminder(
                    body,
                    attemptID: binding.attemptID,
                    payloadDigest: binding.payloadDigest
                )
                commitPhase = "committed_readback"
            case "reminders.complete":
                let body = try BridgeContract.decode(ReminderCompleteRequest.self, from: request.body)
                let binding = try writeBinding(request)
                result = try await eventKit.completeReminder(
                    body,
                    attemptID: binding.attemptID,
                    payloadDigest: binding.payloadDigest
                )
                commitPhase = "committed_readback"
            case "acceptance.calendar.delete":
                let body = try BridgeContract.decode(
                    AcceptanceCalendarDeleteRequest.self,
                    from: request.body
                )
                let binding = try writeBinding(request)
                result = try await eventKit.deleteAcceptanceEvent(
                    body,
                    attemptID: binding.attemptID,
                    payloadDigest: binding.payloadDigest
                )
                commitPhase = "committed_readback"
            case "acceptance.reminder.delete":
                let body = try BridgeContract.decode(
                    AcceptanceReminderDeleteRequest.self,
                    from: request.body
                )
                let binding = try writeBinding(request)
                result = try await eventKit.deleteAcceptanceReminder(
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
        let normalizedDigest = digest.lowercased()
        // Hash the received body rather than a decoded/re-encoded DTO. Python
        // deliberately includes explicit nulls (for example DST choice),
        // while synthesized Swift Encodable omits nil optionals.
        let canonicalBody = try BridgeContract.canonicalJSONData(request.body)
        let computedDigest = SHA256.hash(data: canonicalBody)
            .map { String(format: "%02x", $0) }
            .joined()
        guard normalizedDigest == computedDigest else {
            throw NativeBridgeError(
                "payload_digest_mismatch",
                "payload digest does not match the canonical write body"
            )
        }
        return (attemptID, normalizedDigest)
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
