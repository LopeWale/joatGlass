import AppIntents

// JOAT Brain's actions in the Shortcuts app (App Intents). Each runs in the
// background with the phone locked, and starts JOAT Brain first if it is not
// running, so an automation brings the brain up with no touch on the phone.

/// "Start JOAT Brain": for an automation such as the glasses connecting or
/// the phone coming off the charger.
struct StartBrainIntent: AppIntent {
    static let title: LocalizedStringResource = "Start JOAT Brain"
    static let description = IntentDescription("Starts JOAT Brain in the background so the glasses can connect to it.")
    static let supportedModes: IntentModes = .background
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let wasRunning = JOATRuntime.shared.running
        JOATRuntime.shared.start(reason: "Shortcuts: Start JOAT Brain")
        return .result(value: wasRunning ? "JOAT Brain was already running." : "JOAT Brain started.")
    }
}

/// "Show Text on Glasses": the Message automation hands over each text.
/// JOAT Brain does the rest itself: the sender's name from Contacts, the
/// group, the spam check (2026-11-12: the shortcut had a dozen steps for
/// these; now it is Find Conversations, this, and Mark as Unread).
struct ShowTextIntent: AppIntent {
    static let title: LocalizedStringResource = "Show Text on Glasses"
    static let description = IntentDescription("Shows an incoming text on the glasses. Use it in a Message automation: From is the sender (a number, an email or a name; JOAT Brain finds the name in Contacts), Group is the group chat's name for a group text.")
    static let supportedModes: IntentModes = .background
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(title: "From") var from: String
    @Parameter(title: "Message") var message: String
    @Parameter(title: "Group", description: "The group chat's name, for a text in a group. Empty for one person.")
    var group: String?

    static var parameterSummary: some ParameterSummary {
        Summary("Show \(\.$message) from \(\.$from) on the glasses") {
            \.$group
        }
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        JOATRuntime.shared.start(reason: "Shortcuts: Show Text on Glasses")
        guard let messages = JOATRuntime.shared.brain?.messages else { return .result() }
        // Returned before the spam check finished, iOS could suspend JOAT
        // Brain first: the text was neither saved nor shown (2026-11-09).
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            messages.arrived(from: from, body: message, group: group) { cont.resume() }
        }
        return .result()
    }
}

/// "Add Group to Text": after Show Text on Glasses and Find Conversation, the
/// group the text arrived in. Show Text on Glasses comes first so a text
/// reaches the glasses even when Find Conversation fails (2026-11-14).
struct TextGroupIntent: AppIntent {
    static let title: LocalizedStringResource = "Add Group to Text"
    static let description = IntentDescription("Tells the glasses which group chat the text just shown arrived in. Put it after Show Text on Glasses and Find Conversation, with the conversation's Group Name.")
    static let supportedModes: IntentModes = .background
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(title: "Group", description: "The conversation's Group Name. Empty for one person.")
    var group: String?

    static var parameterSummary: some ParameterSummary {
        Summary("The text just shown is in \(\.$group)")
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        JOATRuntime.shared.start(reason: "Shortcuts: Add Group to Text")
        JOATRuntime.shared.brain?.messages.conversation(group: group)
        return .result()
    }
}

/// What "Get Text to Send" hands to Send Message.
struct OutgoingTextEntity: TransientAppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Text to Send"

    @Property(title: "Recipient") var recipient: String
    @Property(title: "Message") var message: String
    @Property(title: "Name") var name: String
    /// The group chat's name for a reply in a group; nil for a 1:1 message.
    /// Returning "" for a 1:1 message caused Shortcuts' "If Group has any
    /// value" condition to evaluate true, routing individual replies into
    /// Find Conversation instead of the recipient's direct handle.
    @Property(title: "Group") var group: String?
    /// True only for a group chat, for an If that compares a yes/no.
    @Property(title: "Is Group") var isGroup: Bool

    init() {
        recipient = ""
        message = ""
        name = ""
        group = nil
        isGroup = false
    }

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(message)", subtitle: "To \(name)")
    }
}

enum MessagesIntentError: Error, CustomLocalizedStringResourceConvertible {
    case nothingToSend

    var localizedStringResource: LocalizedStringResource {
        switch self {
        case .nothingToSend: "No text is waiting to be sent."
        }
    }
}

/// "Get Text to Send": the text the wearer confirmed on the glasses. Stops
/// the shortcut when there is none, so nothing empty is ever sent.
struct NextTextIntent: AppIntent {
    static let title: LocalizedStringResource = "Get Text to Send"
    static let description = IntentDescription("The text you confirmed on the glasses, for Send Message. Use it in a Notification automation for JOAT Brain.")
    static let supportedModes: IntentModes = .background
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<OutgoingTextEntity> {
        JOATRuntime.shared.start(reason: "Shortcuts: Get Text to Send")
        guard let item = JOATRuntime.shared.brain?.messages.nextOutgoing() else {
            throw MessagesIntentError.nothingToSend
        }
        var entity = OutgoingTextEntity()
        entity.recipient = item.recipient
        entity.message = item.body
        entity.name = item.label
        // Nil, never "", for one person (see OutgoingTextEntity.group).
        let group = item.group?.trimmingCharacters(in: .whitespacesAndNewlines)
        entity.group = (group?.isEmpty ?? true) ? nil : group
        entity.isGroup = entity.group != nil
        return .result(value: entity)
    }
}

/// "Text Sent on Glasses": after Send Message, the lens says it went.
struct TextSentIntent: AppIntent {
    static let title: LocalizedStringResource = "Text Sent on Glasses"
    static let description = IntentDescription("Tells the glasses the text went out. Put it after Send Message.")
    static let supportedModes: IntentModes = .background
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @MainActor
    func perform() async throws -> some IntentResult {
        JOATRuntime.shared.start(reason: "Shortcuts: Text Sent on Glasses")
        JOATRuntime.shared.brain?.messages.sent()
        return .result()
    }
}
