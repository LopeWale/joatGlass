import Foundation
import EventKit

/// Apple Reminders on the phone, through EventKit — the framework the Mac's
/// PersonalDataBridge uses for the same job. iOS 17+ asks for full access
/// (`requestFullAccessToReminders`, NSRemindersFullAccessUsageDescription):
/// write-only access cannot list or complete reminders.
final class RemindersStore {
    let store = EKEventStore()

    /// Read from iOS every time, not remembered: access granted later (in
    /// Settings, or when the first start was a background launch that could
    /// not show the prompt) works at once (review, 2026-10-30).
    var authorized: Bool { EKEventStore.authorizationStatus(for: .reminder) == .fullAccess }
    static var status: EKAuthorizationStatus { EKEventStore.authorizationStatus(for: .reminder) }

    @discardableResult
    func requestAccess() async -> Bool {
        if !authorized {
            do { _ = try await store.requestFullAccessToReminders() }
            catch { Log.add("reminders access error: \(error)") }
            // Granted after this store was made: it must read the new state.
            if authorized { store.reset() }
        }
        Log.add("reminders access: \(authorized ? "full" : "not granted (\(Self.status.rawValue))")")
        return authorized
    }

    /// Open reminders across every list, soonest due first, undated last.
    func incomplete() async -> [EKReminder] {
        guard authorized else { return [] }
        let predicate = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil)
        let found: [EKReminder] = await withCheckedContinuation { c in
            store.fetchReminders(matching: predicate) { c.resume(returning: $0 ?? []) }
        }
        return found.sorted { a, b in
            switch (a.dueDate, b.dueDate) {
            case let (x?, y?): return x < y
            case (_?, nil): return true
            case (nil, _?): return false
            default: return (a.creationDate ?? .distantPast) < (b.creationDate ?? .distantPast)
            }
        }
    }

    /// Open reminders due in [start, end), across every list.
    func incomplete(dueFrom start: Date, until end: Date) async -> [EKReminder] {
        guard authorized else { return [] }
        let predicate = store.predicateForIncompleteReminders(withDueDateStarting: start, ending: end, calendars: nil)
        return await withCheckedContinuation { c in
            store.fetchReminders(matching: predicate) { c.resume(returning: $0 ?? []) }
        }
    }

    /// Creates one reminder in the default reminders list. A due time gets an
    /// alarm at that moment, so iOS itself notifies even if JOAT is not running.
    func create(title: String, due: DateComponents?, notes: String? = nil) throws -> EKReminder {
        guard authorized else { throw NSError(domain: "JOATReminders", code: 1,
                                              userInfo: [NSLocalizedDescriptionKey: "Reminders access not granted"]) }
        guard let list = store.defaultCalendarForNewReminders() else {
            throw NSError(domain: "JOATReminders", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "No default Reminders list on this phone"])
        }
        let r = EKReminder(eventStore: store)
        r.calendar = list
        r.title = title
        r.notes = notes
        if let due {
            r.dueDateComponents = due
            if due.hour != nil, let date = Calendar.current.date(from: due) {
                r.addAlarm(EKAlarm(absoluteDate: date))
            }
        }
        try store.save(r, commit: true)
        Log.add("reminder created in '\(list.title)': \(title) due=\(Self.describe(due))")
        return r
    }

    func complete(_ reminder: EKReminder) throws {
        reminder.isCompleted = true
        try store.save(reminder, commit: true)
        Log.add("reminder completed: \(reminder.title ?? "")")
    }

    func reminder(withIdentifier id: String) -> EKReminder? {
        store.calendarItem(withIdentifier: id) as? EKReminder
    }

    static func describe(_ due: DateComponents?) -> String {
        guard let due, let date = Calendar.current.date(from: due) else { return "none" }
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = due.hour == nil ? .none : .short
        return f.string(from: date)
    }
}

extension EKReminder {
    /// The due moment, resolved in the components' own time zone. Reminders
    /// hands back components without a calendar, so `.date` alone is nil.
    var dueDate: Date? { dueDateComponents.flatMap { Calendar.current.date(from: $0) } }
}
