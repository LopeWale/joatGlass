import Contacts
import Foundation

/// Read-only Contacts lookups for the assistant ("what's Ada's number?").
///
/// The app is the TCC-responsible process, so the permission prompt is shown
/// only from the Mac UI (`requestAccessFromMacUI`), never from bridge IPC,
/// matching the EventKit rule. Nothing here creates, edits or deletes.
actor ContactsService {
    private let store = CNContactStore()

    nonisolated static func authorization() -> NativeAuthorization {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .notDetermined: .notDetermined
        case .restricted: .restricted
        case .denied: .denied
        case .authorized: .fullAccess
        // "Limited" access (macOS 15+): only the contacts the wearer chose.
        case .limited: .fullAccess
        @unknown default: .unknown
        }
    }

    func status() -> ContactsStatusDTO {
        ContactsStatusDTO(contacts: Self.authorization())
    }

    func requestAccessFromMacUI() async throws -> ContactsStatusDTO {
        _ = try await store.requestAccess(for: .contacts)
        return status()
    }

    func search(_ request: ContactsSearchRequest) throws -> [ContactDTO] {
        guard Self.authorization() == .fullAccess else {
            throw NativeBridgeError("contacts_not_authorized", "Contacts access is required")
        }
        let query = request.query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty, query.count <= 100 else {
            throw NativeBridgeError("invalid_query", "query must be 1 to 100 characters")
        }
        let limit = max(1, min(request.limit ?? 10, 25))
        let keys: [CNKeyDescriptor] = [
            CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactJobTitleKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
            CNContactBirthdayKey as CNKeyDescriptor,
            CNContactNicknameKey as CNKeyDescriptor,
        ]
        var predicates = [CNContact.predicateForContacts(matchingName: query)]
        if query.contains("@") {
            predicates.append(CNContact.predicateForContacts(matchingEmailAddress: query))
        }
        let digits = query.filter(\.isNumber)
        if digits.count >= 4 {
            predicates.append(CNContact.predicateForContacts(
                matching: CNPhoneNumber(stringValue: query)))
        }
        var seen = Set<String>()
        var found: [ContactDTO] = []
        for predicate in predicates {
            for contact in try store.unifiedContacts(matching: predicate, keysToFetch: keys) {
                guard seen.insert(contact.identifier).inserted else { continue }
                found.append(ContactDTO(contact))
                if found.count >= limit { return found }
            }
        }
        return found
    }
}

struct ContactsStatusDTO: Codable, Equatable, Sendable {
    let contacts: NativeAuthorization
}

struct ContactsSearchRequest: Codable, Sendable {
    let query: String
    let limit: Int?
}

struct ContactValueDTO: Codable, Equatable, Sendable {
    let label: String
    let value: String
}

struct ContactDTO: Codable, Equatable, Sendable {
    let name: String
    let nickname: String
    let organization: String
    let jobTitle: String
    let phones: [ContactValueDTO]
    let emails: [ContactValueDTO]
    /// "MM-DD" or "YYYY-MM-DD"; empty when unknown.
    let birthday: String

    init(_ contact: CNContact) {
        name = CNContactFormatter.string(from: contact, style: .fullName) ?? ""
        nickname = contact.nickname
        organization = contact.organizationName
        jobTitle = contact.jobTitle
        phones = contact.phoneNumbers.map {
            ContactValueDTO(label: Self.label($0.label), value: $0.value.stringValue)
        }
        emails = contact.emailAddresses.map {
            ContactValueDTO(label: Self.label($0.label), value: $0.value as String)
        }
        if let day = contact.birthday, let month = day.month, let date = day.day {
            birthday = day.year.map { String(format: "%04d-%02d-%02d", $0, month, date) }
                ?? String(format: "%02d-%02d", month, date)
        } else {
            birthday = ""
        }
    }

    private static func label(_ raw: String?) -> String {
        guard let raw else { return "" }
        return CNLabeledValue<NSString>.localizedString(forLabel: raw)
    }
}
