import EventKit
import Foundation

private final class EventStoreChangeObservation: @unchecked Sendable {
    private let lock = NSLock()
    private var token: NSObjectProtocol?

    func install(for store: EKEventStore, handler: @escaping @Sendable () -> Void) {
        lock.withLock {
            guard token == nil else { return }
            token = NotificationCenter.default.addObserver(
                forName: .EKEventStoreChanged,
                object: store,
                queue: nil
            ) { _ in handler() }
        }
    }

    deinit {
        if let token = lock.withLock({ token }) {
            NotificationCenter.default.removeObserver(token)
        }
    }
}

struct NativeBridgeError: Error, Sendable {
    let code: String
    let message: String
    let retryable: Bool
    let commitPhase: String

    init(
        _ code: String,
        _ message: String,
        retryable: Bool = false,
        commitPhase: String = "none"
    ) {
        self.code = code
        self.message = message
        self.retryable = retryable
        self.commitPhase = commitPhase
    }
}

actor EventKitService {
    let storeInstanceID = UUID().uuidString
    private(set) var storeGeneration: UInt64 = 0

    private let store: EKEventStore
    private let changeObservation = EventStoreChangeObservation()
    private var completedAttempts: [String: (payloadDigest: String, result: JSONValue)] = [:]

    init(store: EKEventStore = EKEventStore()) {
        self.store = store
    }

    func startObservingChanges() {
        changeObservation.install(for: store) { [weak self] in
            Task { await self?.invalidate() }
        }
    }

    func invalidate() {
        storeGeneration &+= 1
    }

    func authorizationSnapshot() -> EventKitAuthorizationSnapshot {
        EventKitAuthorizationSnapshot(
            calendar: NativeAuthorization(EKEventStore.authorizationStatus(for: .event)),
            reminders: NativeAuthorization(EKEventStore.authorizationStatus(for: .reminder))
        )
    }

    func capabilities() -> EventKitCapabilities {
        let auth = authorizationSnapshot()
        return EventKitCapabilities(
            calendarRead: auth.calendar == .fullAccess,
            calendarCreate: auth.calendar == .fullAccess,
            remindersRead: auth.reminders == .fullAccess,
            remindersCreate: auth.reminders == .fullAccess,
            remindersComplete: auth.reminders == .fullAccess,
            calendarRecurrenceCreate: false,
            reminderRecurrenceCreate: false
        )
    }

    func requestFullAccess(entity: String) async throws -> EventKitAuthorizationSnapshot {
        switch entity {
        case "calendar":
            _ = try await store.requestFullAccessToEvents()
        case "reminders":
            _ = try await store.requestFullAccessToReminders()
        default:
            throw NativeBridgeError("invalid_entity", "entity must be calendar or reminders")
        }
        // The callback Boolean is not authoritative. Reset and re-read the
        // enum so objects queried before a permission transition cannot leak
        // stale empty results into the first authorized request.
        store.reset()
        storeGeneration &+= 1
        return authorizationSnapshot()
    }

    func listContainers(entity: String) throws -> [EventKitContainerDTO] {
        let entityType = try requireFullAccess(entity)
        let defaultIdentifier = entityType == .event
            ? store.defaultCalendarForNewEvents?.calendarIdentifier
            : store.defaultCalendarForNewReminders()?.calendarIdentifier
        return store.calendars(for: entityType).map { calendar in
            EventKitContainerDTO(
                calendarIdentifier: calendar.calendarIdentifier,
                title: calendar.title,
                sourceIdentifier: calendar.source.sourceIdentifier,
                sourceTitle: calendar.source.title,
                entityTypes: entityType == .event ? ["calendar"] : ["reminders"],
                allowsContentModifications: calendar.allowsContentModifications,
                isSubscribed: calendar.isSubscribed,
                isDefault: calendar.calendarIdentifier == defaultIdentifier
            )
        }.sorted {
            ($0.sourceTitle, $0.title, $0.calendarIdentifier)
                < ($1.sourceTitle, $1.title, $1.calendarIdentifier)
        }
    }

    func queryEvents(_ request: CalendarQueryRequest) throws -> [CalendarEventDTO] {
        _ = try requireFullAccess("calendar")
        guard request.endUnixMS > request.startUnixMS else {
            throw NativeBridgeError("invalid_time_range", "event query end must follow start")
        }
        guard (1...500).contains(request.limit) else {
            throw NativeBridgeError("invalid_limit", "event query limit must be 1 through 500")
        }
        let calendars = try exactCalendars(
            identifiers: request.calendarIdentifiers,
            entity: .event,
            requireWritable: false
        )
        let predicate = store.predicateForEvents(
            withStart: date(request.startUnixMS),
            end: date(request.endUnixMS),
            calendars: calendars.isEmpty ? nil : calendars
        )
        return store.events(matching: predicate)
            .sorted { ($0.startDate, $0.eventIdentifier ?? "") < ($1.startDate, $1.eventIdentifier ?? "") }
            .prefix(request.limit)
            .map(normalize(event:))
    }

    func createEvent(
        _ request: CalendarCreateRequest,
        attemptID: String,
        payloadDigest: String
    ) throws -> JSONValue {
        if let previous = completedAttempts[attemptID] {
            guard previous.payloadDigest == payloadDigest else {
                throw NativeBridgeError("attempt_conflict", "attempt ID was used for another payload")
            }
            return previous.result
        }
        try requireCurrentStore(request.storeInstanceID, request.storeGeneration)
        _ = try requireFullAccess("calendar")
        guard !request.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw NativeBridgeError("invalid_title", "event title is required")
        }
        guard request.endUnixMS > request.startUnixMS else {
            throw NativeBridgeError("invalid_time_range", "event end must follow start")
        }
        let calendar = try exactCalendar(
            identifier: request.calendarIdentifier,
            entity: .event,
            requireWritable: true
        )
        let event = EKEvent(eventStore: store)
        event.calendar = calendar
        event.title = request.title
        event.isAllDay = request.isAllDay
        event.startDate = date(request.startUnixMS)
        event.endDate = date(request.endUnixMS)
        if let zoneID = request.timeZoneID {
            guard let zone = TimeZone(identifier: zoneID) else {
                throw NativeBridgeError("invalid_time_zone", "event time zone is unknown")
            }
            event.timeZone = zone
        } else if !request.isAllDay {
            throw NativeBridgeError("floating_time_excluded", "timed events require an IANA time zone")
        }
        event.alarms = try normalizedAlarms(request.alarmOffsetsSeconds)
        try store.save(event, span: .thisEvent, commit: true)
        storeGeneration &+= 1
        guard let identifier = event.eventIdentifier,
              let readBack = store.event(withIdentifier: identifier)
        else {
            throw NativeBridgeError(
                "commit_readback_missing",
                "EventKit accepted the save but read-back is unavailable",
                retryable: false,
                commitPhase: "committed_unverified"
            )
        }
        let result = try BridgeContract.encode(normalize(event: readBack))
        completedAttempts[attemptID] = (payloadDigest, result)
        return result
    }

    func createReminder(
        _ request: ReminderCreateRequest,
        attemptID: String,
        payloadDigest: String
    ) throws -> JSONValue {
        if let previous = completedAttempts[attemptID] {
            guard previous.payloadDigest == payloadDigest else {
                throw NativeBridgeError("attempt_conflict", "attempt ID was used for another payload")
            }
            return previous.result
        }
        try requireCurrentStore(request.storeInstanceID, request.storeGeneration)
        _ = try requireFullAccess("reminders")
        guard !request.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw NativeBridgeError("invalid_title", "reminder title is required")
        }
        let calendar = try exactCalendar(
            identifier: request.calendarIdentifier,
            entity: .reminder,
            requireWritable: true
        )
        let reminder = EKReminder(eventStore: store)
        reminder.calendar = calendar
        reminder.title = request.title
        var resolvedDueUnixMS: Int64?
        if let due = request.due {
            let resolution = LocalTimeResolver.resolve(due)
            guard resolution.status == .unique,
                  let chosen = resolution.chosenUnixMS,
                  let zone = TimeZone(identifier: due.timeZoneID)
            else {
                throw NativeBridgeError("unresolved_local_time", "reminder due time is not uniquely resolved")
            }
            guard request.resolvedDueUnixMS == chosen else {
                throw NativeBridgeError(
                    "stale_time_resolution",
                    "reminder due instant no longer matches the confirmed request"
                )
            }
            resolvedDueUnixMS = chosen
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = zone
            reminder.dueDateComponents = calendar.dateComponents(
                [.year, .month, .day, .hour, .minute, .second, .timeZone],
                from: date(chosen)
            )
        }
        reminder.alarms = try normalizedReminderAlarms(
            request.alarmOffsetsSeconds,
            dueUnixMS: resolvedDueUnixMS
        )
        try store.save(reminder, commit: true)
        storeGeneration &+= 1
        let normalized = normalize(reminder: reminder)
        let result = try BridgeContract.encode(normalized)
        completedAttempts[attemptID] = (payloadDigest, result)
        return result
    }

    func queryReminders(_ request: ReminderQueryRequest) async throws -> [ReminderDTO] {
        _ = try requireFullAccess("reminders")
        guard (1...500).contains(request.limit) else {
            throw NativeBridgeError("invalid_limit", "reminder query limit must be 1 through 500")
        }
        let calendars = try exactCalendars(
            identifiers: request.calendarIdentifiers,
            entity: .reminder,
            requireWritable: false
        )
        let predicate = store.predicateForReminders(in: calendars.isEmpty ? nil : calendars)
        let fetch = CancellableEventKitFetch<UncheckedEventKitTransfer<[EKReminder]>>(
            cancel: { identifier in
                let transferredIdentifier = UncheckedEventKitTransfer(value: identifier)
                Task { await self.cancelReminderFetch(transferredIdentifier) }
            },
            nilResultError: NativeBridgeError(
                "reminder_fetch_unknown",
                "EventKit returned no reminder result"
            )
        )
        let transferredReminders = try await withTaskCancellationHandler {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { continuation in
                guard fetch.install(continuation) else {
                    continuation.resume(throwing: CancellationError())
                    return
                }
                let identifier = store.fetchReminders(matching: predicate) { reminders in
                    fetch.finish(with: reminders.map(UncheckedEventKitTransfer.init(value:)))
                }
                fetch.install(fetchIdentifier: identifier)
            }
        } onCancel: {
            fetch.cancel()
        }
        let reminders = transferredReminders.value
        return reminders
            .filter { request.includeCompleted || !$0.isCompleted }
            .sorted { reminderSortKey($0) < reminderSortKey($1) }
            .prefix(request.limit)
            .map(normalize(reminder:))
    }

    private func cancelReminderFetch(_ identifier: UncheckedEventKitTransfer<Any>) {
        store.cancelFetchRequest(identifier.value)
    }

    func completeReminder(
        _ request: ReminderCompleteRequest,
        attemptID: String,
        payloadDigest: String
    ) throws -> JSONValue {
        if let previous = completedAttempts[attemptID] {
            guard previous.payloadDigest == payloadDigest else {
                throw NativeBridgeError("attempt_conflict", "attempt ID was used for another payload")
            }
            return previous.result
        }
        try requireCurrentStore(request.storeInstanceID, request.storeGeneration)
        _ = try requireFullAccess("reminders")
        guard let reminder = store.calendarItem(
            withIdentifier: request.calendarItemIdentifier
        ) as? EKReminder else {
            throw NativeBridgeError("reminder_not_found", "reminder no longer exists")
        }
        guard reminder.calendar.calendarIdentifier == request.calendarIdentifier else {
            throw NativeBridgeError(
                "reminder_container_changed",
                "reminder moved to another list after confirmation"
            )
        }
        guard reminder.calendar.allowsContentModifications else {
            throw NativeBridgeError("container_read_only", "reminder list is no longer writable")
        }
        guard !reminder.isCompleted else {
            let result = try BridgeContract.encode(normalize(reminder: reminder))
            completedAttempts[attemptID] = (payloadDigest, result)
            return result
        }
        reminder.isCompleted = true
        try store.save(reminder, commit: true)
        storeGeneration &+= 1
        guard let readBack = store.calendarItem(
            withIdentifier: request.calendarItemIdentifier
        ) as? EKReminder else {
            throw NativeBridgeError(
                "commit_readback_missing",
                "completion was saved but the reminder is not yet readable",
                commitPhase: "committed_unverified"
            )
        }
        let result = try BridgeContract.encode(normalize(reminder: readBack))
        completedAttempts[attemptID] = (payloadDigest, result)
        return result
    }

    func deleteAcceptanceEvent(
        _ request: AcceptanceCalendarDeleteRequest,
        attemptID: String,
        payloadDigest: String
    ) throws -> JSONValue {
        if let previous = completedAttempts[attemptID] {
            guard previous.payloadDigest == payloadDigest else {
                throw NativeBridgeError("attempt_conflict", "attempt ID was used for another payload")
            }
            return previous.result
        }
        try requireAcceptanceTitle(request.expectedTitle, suffix: "EVENT")
        _ = try requireFullAccess("calendar")
        guard let event = store.calendarItem(
            withIdentifier: request.calendarItemIdentifier
        ) as? EKEvent else {
            throw NativeBridgeError("acceptance_item_not_found", "synthetic event no longer exists")
        }
        guard event.calendar.calendarIdentifier == request.calendarIdentifier,
              event.title == request.expectedTitle,
              unixMS(event.startDate) == request.expectedStartUnixMS,
              unixMS(event.endDate) == request.expectedEndUnixMS,
              (event.recurrenceRules ?? []).isEmpty
        else {
            throw NativeBridgeError(
                "acceptance_item_changed",
                "synthetic event no longer matches its read-back identity"
            )
        }
        guard event.calendar.allowsContentModifications else {
            throw NativeBridgeError("container_read_only", "synthetic event calendar is read-only")
        }
        do {
            try store.remove(event, span: .thisEvent, commit: true)
        } catch {
            throw NativeBridgeError(
                "acceptance_cleanup_failed",
                "EventKit did not confirm synthetic event removal",
                commitPhase: "committed_unverified"
            )
        }
        storeGeneration &+= 1
        guard store.calendarItem(withIdentifier: request.calendarItemIdentifier) == nil else {
            throw NativeBridgeError(
                "acceptance_cleanup_readback_failed",
                "synthetic event remained readable after removal",
                commitPhase: "committed_unverified"
            )
        }
        let result = try BridgeContract.encode(AcceptanceDeleteResult(removed: true))
        completedAttempts[attemptID] = (payloadDigest, result)
        return result
    }

    func deleteAcceptanceReminder(
        _ request: AcceptanceReminderDeleteRequest,
        attemptID: String,
        payloadDigest: String
    ) throws -> JSONValue {
        if let previous = completedAttempts[attemptID] {
            guard previous.payloadDigest == payloadDigest else {
                throw NativeBridgeError("attempt_conflict", "attempt ID was used for another payload")
            }
            return previous.result
        }
        try requireAcceptanceTitle(request.expectedTitle, suffix: "REMINDER")
        _ = try requireFullAccess("reminders")
        guard let reminder = store.calendarItem(
            withIdentifier: request.calendarItemIdentifier
        ) as? EKReminder else {
            throw NativeBridgeError("acceptance_item_not_found", "synthetic reminder no longer exists")
        }
        guard reminder.calendar.calendarIdentifier == request.calendarIdentifier,
              reminder.title == request.expectedTitle,
              reminderDueUnixMS(reminder) == request.expectedDueUnixMS,
              (reminder.recurrenceRules ?? []).isEmpty
        else {
            throw NativeBridgeError(
                "acceptance_item_changed",
                "synthetic reminder no longer matches its read-back identity"
            )
        }
        guard reminder.calendar.allowsContentModifications else {
            throw NativeBridgeError("container_read_only", "synthetic reminder list is read-only")
        }
        do {
            try store.remove(reminder, commit: true)
        } catch {
            throw NativeBridgeError(
                "acceptance_cleanup_failed",
                "EventKit did not confirm synthetic reminder removal",
                commitPhase: "committed_unverified"
            )
        }
        storeGeneration &+= 1
        guard store.calendarItem(withIdentifier: request.calendarItemIdentifier) == nil else {
            throw NativeBridgeError(
                "acceptance_cleanup_readback_failed",
                "synthetic reminder remained readable after removal",
                commitPhase: "committed_unverified"
            )
        }
        let result = try BridgeContract.encode(AcceptanceDeleteResult(removed: true))
        completedAttempts[attemptID] = (payloadDigest, result)
        return result
    }

    private func requireCurrentStore(_ instanceID: String, _ generation: UInt64) throws {
        guard instanceID == storeInstanceID else {
            throw NativeBridgeError("stale_store_instance", "native bridge restarted; preview is stale")
        }
        guard generation == storeGeneration else {
            throw NativeBridgeError("stale_store_generation", "EventKit changed; preview is stale")
        }
    }

    private func requireAcceptanceTitle(_ title: String, suffix: String) throws {
        let components = title.split(separator: " ", omittingEmptySubsequences: false)
        guard components.count == 4,
              components[0] == "JOAT",
              components[1] == "ACCEPTANCE",
              components[2].count == 8,
              components[2].allSatisfy({ "0123456789abcdef".contains($0) }),
              components[3] == Substring(suffix)
        else {
            throw NativeBridgeError(
                "acceptance_title_required",
                "native cleanup is restricted to randomized acceptance items"
            )
        }
    }

    private func requireFullAccess(_ entity: String) throws -> EKEntityType {
        let type: EKEntityType
        switch entity {
        case "calendar": type = .event
        case "reminders": type = .reminder
        default: throw NativeBridgeError("invalid_entity", "unknown EventKit entity")
        }
        let status = NativeAuthorization(EKEventStore.authorizationStatus(for: type))
        guard status == .fullAccess else {
            throw NativeBridgeError("full_access_required", "\(entity) full access is required")
        }
        return type
    }

    private func exactCalendars(
        identifiers: [String],
        entity: EKEntityType,
        requireWritable: Bool
    ) throws -> [EKCalendar] {
        try identifiers.map {
            try exactCalendar(identifier: $0, entity: entity, requireWritable: requireWritable)
        }
    }

    private func exactCalendar(
        identifier: String,
        entity: EKEntityType,
        requireWritable: Bool
    ) throws -> EKCalendar {
        guard let calendar = store.calendar(withIdentifier: identifier) else {
            throw NativeBridgeError("container_not_found", "selected calendar/list no longer exists")
        }
        guard calendar.allowedEntityTypes.contains(entity == .event ? .event : .reminder) else {
            throw NativeBridgeError("container_wrong_entity", "selected container has the wrong entity type")
        }
        if requireWritable && !calendar.allowsContentModifications {
            throw NativeBridgeError("container_read_only", "selected calendar/list is not writable")
        }
        return calendar
    }

    private func normalizedAlarms(_ secondsBefore: [Int]) throws -> [EKAlarm] {
        guard secondsBefore.count <= 8,
              secondsBefore.allSatisfy({ $0 >= 0 && $0 <= 31_536_000 })
        else { throw NativeBridgeError("invalid_alarms", "alarm offsets are outside V1 bounds") }
        return Array(Set(secondsBefore)).sorted().map {
            EKAlarm(relativeOffset: -TimeInterval($0))
        }
    }

    private func normalizedReminderAlarms(
        _ secondsBefore: [Int],
        dueUnixMS: Int64?
    ) throws -> [EKAlarm] {
        guard secondsBefore.count <= 8,
              secondsBefore.allSatisfy({ $0 >= 0 && $0 <= 31_536_000 })
        else { throw NativeBridgeError("invalid_alarms", "alarm offsets are outside V1 bounds") }
        if secondsBefore.isEmpty { return [] }
        guard let dueUnixMS else {
            throw NativeBridgeError("invalid_alarms", "reminder alarms require an exact due time")
        }
        return Array(Set(secondsBefore)).sorted().map { seconds in
            EKAlarm(absoluteDate: date(dueUnixMS - Int64(seconds) * 1_000))
        }
    }

    private func normalize(event: EKEvent) -> CalendarEventDTO {
        CalendarEventDTO(
            calendarItemIdentifier: event.calendarItemIdentifier,
            calendarItemExternalIdentifier: event.calendarItemExternalIdentifier,
            calendarIdentifier: event.calendar.calendarIdentifier,
            sourceIdentifier: event.calendar.source.sourceIdentifier,
            title: event.title ?? "",
            isAllDay: event.isAllDay,
            startUnixMS: unixMS(event.startDate),
            endUnixMS: unixMS(event.endDate),
            timeZoneID: event.timeZone?.identifier,
            alarms: normalize(alarms: event.alarms),
            hasRecurrenceRules: !(event.recurrenceRules ?? []).isEmpty,
            createdUnixMS: event.creationDate.map(unixMS),
            modifiedUnixMS: event.lastModifiedDate.map(unixMS)
        )
    }

    private func normalize(reminder: EKReminder) -> ReminderDTO {
        ReminderDTO(
            calendarItemIdentifier: reminder.calendarItemIdentifier,
            calendarItemExternalIdentifier: reminder.calendarItemExternalIdentifier,
            calendarIdentifier: reminder.calendar.calendarIdentifier,
            sourceIdentifier: reminder.calendar.source.sourceIdentifier,
            title: reminder.title ?? "",
            due: reminder.dueDateComponents.flatMap(civilTime),
            alarms: normalize(alarms: reminder.alarms),
            isCompleted: reminder.isCompleted,
            completionUnixMS: reminder.completionDate.map(unixMS),
            hasRecurrenceRules: !(reminder.recurrenceRules ?? []).isEmpty,
            priority: reminder.priority
        )
    }

    private func reminderSortKey(_ reminder: EKReminder) -> (Date, String) {
        let due = reminder.dueDateComponents.flatMap { components -> Date? in
            var calendar = components.calendar ?? Calendar(identifier: .gregorian)
            if let zone = components.timeZone { calendar.timeZone = zone }
            return calendar.date(from: components)
        } ?? Date.distantFuture
        return (due, reminder.calendarItemIdentifier)
    }

    private func reminderDueUnixMS(_ reminder: EKReminder) -> Int64? {
        reminder.dueDateComponents.flatMap { components in
            var calendar = components.calendar ?? Calendar(identifier: .gregorian)
            if let zone = components.timeZone { calendar.timeZone = zone }
            return calendar.date(from: components).map(unixMS)
        }
    }

    private func civilTime(_ components: DateComponents) -> CivilTime? {
        guard let year = components.year,
              let month = components.month,
              let day = components.day,
              let zone = components.timeZone
        else { return nil }
        return CivilTime(
            year: year,
            month: month,
            day: day,
            hour: components.hour ?? 0,
            minute: components.minute ?? 0,
            second: components.second ?? 0,
            timeZoneID: zone.identifier,
            repeatedTimeChoice: nil
        )
    }

    private func normalize(alarms: [EKAlarm]?) -> [AlarmDTO] {
        (alarms ?? []).map { alarm in
            AlarmDTO(
                relativeOffsetSeconds: alarm.absoluteDate == nil
                    ? Int(alarm.relativeOffset.rounded()) : nil,
                absoluteUnixMS: alarm.absoluteDate.map(unixMS)
            )
        }.sorted {
            ($0.relativeOffsetSeconds ?? Int.max, $0.absoluteUnixMS ?? Int64.max)
                < ($1.relativeOffsetSeconds ?? Int.max, $1.absoluteUnixMS ?? Int64.max)
        }
    }

    private func date(_ unixMS: Int64) -> Date {
        Date(timeIntervalSince1970: TimeInterval(unixMS) / 1_000)
    }

    private func unixMS(_ date: Date) -> Int64 {
        Int64((date.timeIntervalSince1970 * 1_000).rounded())
    }
}
