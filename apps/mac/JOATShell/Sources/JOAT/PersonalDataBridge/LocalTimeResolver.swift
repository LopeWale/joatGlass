import Foundation

struct CivilTime: Codable, Equatable, Sendable {
    let year: Int
    let month: Int
    let day: Int
    let hour: Int
    let minute: Int
    let second: Int
    let timeZoneID: String
    let repeatedTimeChoice: RepeatedTimeChoice?

    enum CodingKeys: String, CodingKey {
        case year, month, day, hour, minute, second, repeatedTimeChoice
        case timeZoneID = "timeZoneId"
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(year, forKey: .year)
        try container.encode(month, forKey: .month)
        try container.encode(day, forKey: .day)
        try container.encode(hour, forKey: .hour)
        try container.encode(minute, forKey: .minute)
        try container.encode(second, forKey: .second)
        try container.encode(timeZoneID, forKey: .timeZoneID)
        if let repeatedTimeChoice {
            try container.encode(repeatedTimeChoice, forKey: .repeatedTimeChoice)
        } else {
            try container.encodeNil(forKey: .repeatedTimeChoice)
        }
    }
}

enum RepeatedTimeChoice: String, Codable, Sendable {
    case first
    case last
}

struct LocalTimeResolution: Codable, Equatable, Sendable {
    enum Status: String, Codable, Sendable {
        case unique
        case ambiguous
        case nonexistent
        case invalidTimeZone
    }

    let status: Status
    let firstUnixMS: Int64?
    let lastUnixMS: Int64?
    let chosenUnixMS: Int64?
    let chosenUTCOffsetSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case status
        case firstUnixMS = "firstUnixMs"
        case lastUnixMS = "lastUnixMs"
        case chosenUnixMS = "chosenUnixMs"
        case chosenUTCOffsetSeconds = "chosenUtcOffsetSeconds"
    }
}

enum LocalTimeResolver {
    static func resolve(_ intent: CivilTime) -> LocalTimeResolution {
        guard let zone = TimeZone(identifier: intent.timeZoneID) else {
            return LocalTimeResolution(
                status: .invalidTimeZone,
                firstUnixMS: nil,
                lastUnixMS: nil,
                chosenUnixMS: nil,
                chosenUTCOffsetSeconds: nil
            )
        }
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = Locale(identifier: "en_US_POSIX")
        calendar.timeZone = zone
        let wanted = DateComponents(
            calendar: calendar,
            timeZone: zone,
            year: intent.year,
            month: intent.month,
            day: intent.day,
            hour: intent.hour,
            minute: intent.minute,
            second: intent.second
        )
        guard let day = calendar.date(from: DateComponents(
            calendar: calendar,
            timeZone: zone,
            year: intent.year,
            month: intent.month,
            day: intent.day
        )) else {
            return missing(.nonexistent)
        }
        let anchor = day.addingTimeInterval(-1)
        let first = calendar.nextDate(
            after: anchor,
            matching: wanted,
            matchingPolicy: .strict,
            repeatedTimePolicy: .first,
            direction: .forward
        )
        let last = calendar.nextDate(
            after: anchor,
            matching: wanted,
            matchingPolicy: .strict,
            repeatedTimePolicy: .last,
            direction: .forward
        )
        guard let first, let last,
              exactlyMatches(first, intent: intent, calendar: calendar),
              exactlyMatches(last, intent: intent, calendar: calendar)
        else { return missing(.nonexistent) }

        let firstMS = unixMS(first)
        let lastMS = unixMS(last)
        if firstMS != lastMS, intent.repeatedTimeChoice == nil {
            return LocalTimeResolution(
                status: .ambiguous,
                firstUnixMS: firstMS,
                lastUnixMS: lastMS,
                chosenUnixMS: nil,
                chosenUTCOffsetSeconds: nil
            )
        }
        let chosen = intent.repeatedTimeChoice == .last ? last : first
        return LocalTimeResolution(
            status: .unique,
            firstUnixMS: firstMS,
            lastUnixMS: lastMS,
            chosenUnixMS: unixMS(chosen),
            chosenUTCOffsetSeconds: zone.secondsFromGMT(for: chosen)
        )
    }

    private static func missing(_ status: LocalTimeResolution.Status) -> LocalTimeResolution {
        LocalTimeResolution(
            status: status,
            firstUnixMS: nil,
            lastUnixMS: nil,
            chosenUnixMS: nil,
            chosenUTCOffsetSeconds: nil
        )
    }

    private static func unixMS(_ date: Date) -> Int64 {
        Int64((date.timeIntervalSince1970 * 1_000).rounded())
    }

    private static func exactlyMatches(
        _ date: Date,
        intent: CivilTime,
        calendar: Calendar
    ) -> Bool {
        let actual = calendar.dateComponents(
            [.year, .month, .day, .hour, .minute, .second],
            from: date
        )
        return actual.year == intent.year
            && actual.month == intent.month
            && actual.day == intent.day
            && actual.hour == intent.hour
            && actual.minute == intent.minute
            && actual.second == intent.second
    }
}
