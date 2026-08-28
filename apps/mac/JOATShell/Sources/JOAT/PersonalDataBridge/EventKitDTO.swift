import EventKit
import Foundation

enum NativeAuthorization: String, Codable, Sendable {
    case notDetermined
    case restricted
    case denied
    case writeOnly
    case fullAccess
    case unknown

    init(_ status: EKAuthorizationStatus) {
        switch status {
        case .notDetermined: self = .notDetermined
        case .restricted: self = .restricted
        case .denied: self = .denied
        case .writeOnly: self = .writeOnly
        case .fullAccess: self = .fullAccess
        @unknown default: self = .unknown
        }
    }
}

struct EventKitAuthorizationSnapshot: Codable, Equatable, Sendable {
    let calendar: NativeAuthorization
    let reminders: NativeAuthorization
}

struct EventKitCapabilities: Codable, Equatable, Sendable {
    let calendarRead: Bool
    let calendarCreate: Bool
    let remindersRead: Bool
    let remindersCreate: Bool
    let remindersComplete: Bool
    let calendarRecurrenceCreate: Bool
    let reminderRecurrenceCreate: Bool
}

struct EventKitContainerDTO: Codable, Equatable, Sendable {
    let calendarIdentifier: String
    let title: String
    let sourceIdentifier: String
    let sourceTitle: String
    let entityTypes: [String]
    let allowsContentModifications: Bool
    let isSubscribed: Bool
    let isDefault: Bool
}

struct AlarmDTO: Codable, Equatable, Sendable {
    let relativeOffsetSeconds: Int?
    let absoluteUnixMS: Int64?

    enum CodingKeys: String, CodingKey {
        case relativeOffsetSeconds
        case absoluteUnixMS = "absoluteUnixMs"
    }
}

struct CalendarEventDTO: Codable, Equatable, Sendable {
    let calendarItemIdentifier: String
    let calendarItemExternalIdentifier: String?
    let calendarIdentifier: String
    let sourceIdentifier: String
    let title: String
    let isAllDay: Bool
    let startUnixMS: Int64
    let endUnixMS: Int64
    let timeZoneID: String?
    let alarms: [AlarmDTO]
    let hasRecurrenceRules: Bool
    let createdUnixMS: Int64?
    let modifiedUnixMS: Int64?

    enum CodingKeys: String, CodingKey {
        case calendarItemIdentifier, calendarItemExternalIdentifier
        case calendarIdentifier, sourceIdentifier, title, isAllDay, alarms
        case hasRecurrenceRules
        case startUnixMS = "startUnixMs"
        case endUnixMS = "endUnixMs"
        case timeZoneID = "timeZoneId"
        case createdUnixMS = "createdUnixMs"
        case modifiedUnixMS = "modifiedUnixMs"
    }
}

struct ReminderDTO: Codable, Equatable, Sendable {
    let calendarItemIdentifier: String
    let calendarItemExternalIdentifier: String?
    let calendarIdentifier: String
    let sourceIdentifier: String
    let title: String
    let due: CivilTime?
    let alarms: [AlarmDTO]
    let isCompleted: Bool
    let completionUnixMS: Int64?
    let hasRecurrenceRules: Bool
    let priority: Int

    enum CodingKeys: String, CodingKey {
        case calendarItemIdentifier, calendarItemExternalIdentifier
        case calendarIdentifier, sourceIdentifier, title, due, alarms
        case isCompleted, hasRecurrenceRules, priority
        case completionUnixMS = "completionUnixMs"
    }
}

struct EntityRequest: Codable, Sendable {
    let entity: String
}

struct AccessRequest: Codable, Sendable {
    let entity: String
    let userInitiated: Bool
}

struct CalendarQueryRequest: Codable, Sendable {
    let startUnixMS: Int64
    let endUnixMS: Int64
    let calendarIdentifiers: [String]
    let limit: Int

    enum CodingKeys: String, CodingKey {
        case calendarIdentifiers, limit
        case startUnixMS = "startUnixMs"
        case endUnixMS = "endUnixMs"
    }
}

struct CalendarCreateRequest: Codable, Sendable {
    let storeInstanceID: String
    let storeGeneration: UInt64
    let calendarIdentifier: String
    let title: String
    let isAllDay: Bool
    let startUnixMS: Int64
    let endUnixMS: Int64
    let timeZoneID: String?
    let alarmOffsetsSeconds: [Int]

    enum CodingKeys: String, CodingKey {
        case storeInstanceID = "storeInstanceId"
        case storeGeneration, calendarIdentifier, title, isAllDay
        case startUnixMS = "startUnixMs"
        case endUnixMS = "endUnixMs"
        case timeZoneID = "timeZoneId"
        case alarmOffsetsSeconds
    }
}

struct ReminderCreateRequest: Codable, Sendable {
    let storeInstanceID: String
    let storeGeneration: UInt64
    let calendarIdentifier: String
    let title: String
    let due: CivilTime?
    let resolvedDueUnixMS: Int64?
    let alarmOffsetsSeconds: [Int]

    enum CodingKeys: String, CodingKey {
        case storeInstanceID = "storeInstanceId"
        case storeGeneration, calendarIdentifier, title, due, alarmOffsetsSeconds
        case resolvedDueUnixMS = "resolvedDueUnixMs"
    }
}

struct ReminderQueryRequest: Codable, Sendable {
    let calendarIdentifiers: [String]
    let includeCompleted: Bool
    let limit: Int
}

struct ReminderCompleteRequest: Codable, Sendable {
    let storeInstanceID: String
    let storeGeneration: UInt64
    let calendarIdentifier: String
    let calendarItemIdentifier: String

    enum CodingKeys: String, CodingKey {
        case storeInstanceID = "storeInstanceId"
        case storeGeneration, calendarIdentifier, calendarItemIdentifier
    }
}

struct LocalTimeResolveRequest: Codable, Sendable {
    let civilTime: CivilTime
}
