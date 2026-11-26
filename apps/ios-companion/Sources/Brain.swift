import Foundation
import EventKit

/// The brain loop on the phone: glasses mic → on-device STT → wake gate →
/// deterministic commands or streaming on-phone/Mac assistant → the glasses'
/// native conversation page + optional spoken reply in headphones.
final class Brain {
    private let server: LoopbackServer
    private let link: PluginLink
    private let stt: Transcriber
    /// Same audio, the other Apple engine: logged only, for the comparison.
    private let shadow: Transcriber?
    let voice: Voice
    private var conversationId = "phone-\(UUID().uuidString.prefix(8).lowercased())"
    private var revision = 0
    /// Turns shown on the current glasses conversation card (`conversationId`).
    private var turns: [[String: String]] = []
    /// Recent conversational turns (kept up to 30 s) so follow-up questions keep
    /// context even after the wearer taps to close the previous card.
    private var modelHistory: [[String: String]] = []
    /// Whether `conversationId` is currently mounted on the glasses lens.
    /// Any exit, dismiss, or lens-claiming feature resets this so the next
    /// question mints a fresh `conversationId` (never hitting `retired` in Hub).
    private var conversationVisible = false
    private var lastAddressedAt = Date.distantPast
    private var streamEpoch = 0
    private var preDismissedEpoch: Int?
    private var streamingTurnIndex: Int?
    private(set) var toolBelt: PhoneToolBelt!
    /// After the name alone ("JOAT" -> "Yes?"), the next utterance is the request.
    private var nameOnlyUntil = Date.distantPast
    private var nameOnlyLatchedUntil = Date.distantPast
    /// orchestrator.py ConversationWindow: after JOAT answers an addressed
    /// request, the next question needs no name for `joat.followUpSeconds`
    /// (0 = off, 8 unless changed). Never hard-coded.
    static let followUpKey = "joat.followUpSeconds"
    static var conversationWindow: TimeInterval {
        max(0, UserDefaults.standard.object(forKey: followUpKey) as? Double ?? 8)
    }
    private var conversationUntil = Date.distantPast
    /// Latched when the wearer begins speaking inside an open follow-up window
    /// so sentences that finish after the countdown still go through.
    private var speechLatchedUntil = Date.distantPast
    private var extendWindowAfterSpeech = false
    /// True while a follow-up needs no "JOAT".
    var followUpWindowOpen: Bool { Date() < conversationUntil }
    static let listeningCue = "Listening… (no name needed)"
    private var cueExpiry: DispatchWorkItem?

    let router = FeatureRouter()
    let hud: Hud
    let panels: PanelPublisher
    let pomodoro = Pomodoro()
    let confirm: ConfirmGate
    let reminders: Reminders
    /// The capability menu's pages (capabilities.py), Planning first.
    let capabilities: CapabilityMenu
    /// JOAT's own notification cards and the dashboard's margin facts.
    let cards: Cards
    /// Voice and text notes; while a note listens, sentences go to it.
    let quickNotes: QuickNotes
    /// The News rail tab and its article reader (services/news.py).
    let news = News()
    /// The Search tab (services/search.py): key-free lookups.
    let search = Search()
    /// The Mac's assistant over the tailnet (services/agent_endpoint.py).
    let macLink = MacLink()
    /// This phone's texts through the home Mac (services/messages_endpoint.py).
    let macMessages = MacMessages()
    /// The Mac's Agent Terminal over the tailnet (services/terminal_endpoint.py).
    let macTerminal: MacTerminal
    /// 3-level Agent Terminal capability on the glasses via `macTerminal`.
    let glassesTerminal: GlassesTerminal
    /// Routes to a place with Apple Maps (services/navigation.py).
    let navigation: Navigation
    /// Texts in and out through the home Mac over Tailscale (Messages.swift).
    let messages: Messages
    /// Standalone multi-account Gmail on this iPhone (Mail.swift).
    let mail: Mail
    /// Questions the glasses ask for a feature (Messages/Mail's "What should I say?").
    let prompts: Prompts
    /// A question JOAT asked ("When should I remind you?"): the next
    /// sentence answers it, no name needed.
    private var followUp: ((String) -> Void)?
    private var followUpUntil = Date.distantPast
    /// The request a command is answering, for the turn its question shows.
    private var currentRequest: String?
    /// The deterministic command chain, ahead of any model (commands.py).
    private var commands: [VoiceCommand] = []

    @MainActor
    init(
        server: LoopbackServer,
        stt: Transcriber,
        shadow: Transcriber?,
        voice: Voice,
        macTerminal: MacTerminal? = nil,
        cardDefaults: UserDefaults = .standard,
        linkOverride: PluginLink? = nil
    ) {
        let activeLink: PluginLink = linkOverride ?? server
        let hasTestLink = linkOverride != nil
        self.server = server
        self.link = activeLink
        let resolvedTerminal = macTerminal ?? MacTerminal()
        self.macTerminal = resolvedTerminal
        hud = Hud(link: activeLink)
        panels = PanelPublisher(link: activeLink)
        confirm = ConfirmGate(link: activeLink)
        reminders = Reminders(link: activeLink, hud: hud, confirm: confirm)
        cards = Cards(link: activeLink, defaults: cardDefaults)
        quickNotes = QuickNotes(link: activeLink, hud: hud)
        navigation = Navigation(link: activeLink)
        messages = Messages(hud: hud, confirm: confirm, cards: cards)
        mail = Mail(hud: hud, confirm: confirm, cards: cards)
        prompts = Prompts(link: activeLink)
        capabilities = CapabilityMenu(link: activeLink, panels: panels,
                                      reminders: reminders,
                                      cards: cards, quickNotes: quickNotes)
        glassesTerminal = GlassesTerminal(
            link: activeLink,
            macTerminal: resolvedTerminal,
            prompts: prompts,
            menu: capabilities
        )
        glassesTerminal.overrideMacTerminal = { AssistantRuntimeHooks.sharedMacTerminal?() }
        quickNotes.menu = capabilities
        capabilities.navigation = navigation
        capabilities.messages = messages
        capabilities.mail = mail
        capabilities.macTerminal = resolvedTerminal
        capabilities.glassesTerminal = glassesTerminal
        capabilities.pomodoro = pomodoro
        messages.onChange = { [capabilities] in capabilities.refreshMessages() }
        messages.onThreadChange = { [capabilities] in capabilities.refreshThread() }
        messages.link = activeLink
        messages.prompts = prompts
        messages.mac = macMessages
        mail.link = activeLink
        mail.prompts = prompts
        mail.say = { [voice] text in voice.say(text) }
        mail.onChange = { [capabilities] in capabilities.refreshMail() }
        mail.onThreadChange = { [capabilities] in capabilities.refreshMailThread() }
        router.register(prompts)
        server.onUplink = { [router] kind, obj, raw in router.route(kind: kind, message: obj, raw: raw) }
        panels.register(pomodoro)
        pomodoro.onChange = { [panels] in panels.publish("pomodoro") }
        pomodoro.onCard = { [cards] source, summary in cards.post(source: source, channel: "Pomodoro", summary: summary) }
        router.register(panels)
        commands.append(capabilities)
        commands.append(mail)
        commands.append(messages)
        messages.say = { [voice] text in voice.say(text) }
        commands.append(SpokenRepliesCommand(voice: voice))
        router.register(navigation)
        commands.append(navigation)
        navigation.prompts = prompts
        navigation.isPluginConnected = { [server] in hasTestLink || server.clients > 0 }
        navigation.sayCue = { [voice] text in voice.sayNavigation(text) }
        navigation.onMenuChanged = { [capabilities] in capabilities.refreshNavigation() }
        navigation.onClaimedGlass = { [capabilities] in capabilities.leaveNavigationIfOpen() }
        navigation.receipt = { [hud, voice] text in
            hud.receipt(text)
            voice.say(text)
        }
        panels.register(news)
        news.publish = { [panels] activate in panels.publish("news", activate: activate) }
        news.sendReader = { [panels, activeLink] list in
            var reader = list
            reader["requestId"] = panels.requestId("news")
            activeLink.send(reader)
            Log.add("tx hud.list NEWS · ARTICLE \(panels.requestId("news"))")
        }
        news.say = { [voice] text in voice.say(text) }
        commands.append(news)
        panels.register(quickNotes)
        quickNotes.publishPanel = { [panels] in panels.publish("quicknotes") }
        quickNotes.say = { [voice] text in voice.say(text) }
        quickNotes.macLink = macLink
        quickNotes.reminders = reminders
        commands.append(quickNotes)
        commands.append(PomodoroCommands(pomodoro: pomodoro, panels: panels))
        panels.register(search)
        search.publish = { [panels] activate in panels.publish("search", activate: activate) }
        search.sendReader = { [panels, activeLink] list in
            var reader = list
            reader["requestId"] = panels.requestId("search")
            activeLink.send(reader)
        }
        search.say = { [voice] text in voice.say(text) }
        commands.append(search)
        router.register(confirm)
        router.register(MicResume(link: activeLink))
        router.register(MessageHold(channels: [messages, mail], connected: { [server] in hasTestLink || server.clients > 0 }))
        router.register(reminders)
        router.register(capabilities)
        router.register(glassesTerminal)
        router.register(cards)
        panels.register(reminders)
        reminders.publishPanel = { [panels] in panels.publish("planner") }
        reminders.say = { [voice] text in voice.say(text) }
        commands.append(reminders)
        self.stt = stt
        self.shadow = shadow
        self.voice = voice
        server.onPluginConnected = { [weak self, router, pomodoro] in
            router.pluginConnected()
            DispatchQueue.main.async {
                self?.rotateCardConversation()
                pomodoro.flushRecoveryCard()
            }
        }
        server.onAudio = { [weak stt, weak shadow, quickNotes] samples in
            stt?.feed(samples)
            shadow?.feed(samples)
            DispatchQueue.main.async { quickNotes.audioFrame(samples) }
        }
        stt.onEvent = { [weak self] event in DispatchQueue.main.async { self?.handle(event) } }
        voice.onFinishSpeaking = { [weak self] in self?.refreshFollowUpAfterSpeechIfNeeded() }
        reminders.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        reminders.onListAnswered = { [weak self] req, ans in self?.publishAnsweredCommand(user: req, assistant: ans) }
        search.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        navigation.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        messages.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        mail.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        router.register(ConversationInput { [weak self] action in self?.conversationChoice(action) })
        macLink.claude.area = { [navigation] in await navigation.area() }
        navigation.onAsked = { [weak self] question in self?.publish(user: question, assistant: nil, state: "answering") }
        navigation.onAnswer = { [weak self] answer in
            self?.publish(user: nil, assistant: answer, state: "ready")
            self?.voice.say(answer)
        }
        macMessages.start(deliver: { [weak self] text, done in
            guard let self else { done(); return }
            self.messages.arrivedFromMac(text, done: done)
        })
        toolBelt = PhoneToolBelt(
            brain: self,
            dismissConversation: { [weak self] in
                self?.dismissActiveStreamingConversation()
            }
        )
    }

    private var insightsTimer: Timer?

    func start() {
        stt.start()
        shadow?.start()
        reminders.start()
        news.start()
        mail.start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [quickNotes] in quickNotes.retryPendingInsights() }
        insightsTimer = Timer.scheduledTimer(withTimeInterval: 600, repeats: true) { [quickNotes] _ in
            quickNotes.retryPendingInsights()
        }
    }

    /// Mints a fresh `conversationId` for the next glasses card while preserving
    /// `modelHistory` (within 30 s) so follow-ups keep conversational context
    /// without ever colliding with `AssistantConversationSurface.retired`.
    private func rotateCardConversation() {
        cueExpiry?.cancel()
        cueExpiry = nil
        extendWindowAfterSpeech = false
        conversationId = "phone-\(UUID().uuidString.prefix(8).lowercased())"
        revision = 0
        turns = []
        streamingTurnIndex = nil
        conversationVisible = false
    }

    private func prepareConversationSession(now: Date) {
        if now.timeIntervalSince(lastAddressedAt) > 30 {
            modelHistory = []
            rotateCardConversation()
        } else if !conversationVisible || turns.isEmpty {
            rotateCardConversation()
        }
    }

    private func appendModelHistory(role: String, text: String) {
        let clipped = String(text.prefix(3900))
        guard !clipped.isEmpty else { return }
        modelHistory.append(["role": role, "text": clipped])
        if modelHistory.count > 16 {
            modelHistory.removeFirst(modelHistory.count - 16)
        }
    }

    /// Ask, show and say a question; the next sentence is its answer.
    private func ask(_ question: String, answer: @escaping (String) -> Void) {
        prepareConversationSession(now: Date())
        followUp = answer
        followUpUntil = Date().addingTimeInterval(15)
        nameOnlyUntil = .distantPast
        nameOnlyLatchedUntil = .distantPast
        lastAddressedAt = Date()
        publish(user: currentRequest, assistant: question, state: "ready")
        currentRequest = nil
        voice.say(question)
    }

    private static let confirmCancelPhrases: Set<String> = [
        "cancel", "no", "nope", "never mind", "nevermind", "stop",
        "don't send", "do not send", "cancel it", "don't", "do not",
    ]

    private static let confirmAcceptPhrases: Set<String> = [
        "yes", "yeah", "yep", "confirm", "send", "send it", "approve", "do it",
    ]

    private static func normalizeConfirmSpeech(_ raw: String) -> String {
        raw.lowercased()
            .replacingOccurrences(of: "’", with: "'")
            .replacingOccurrences(of: "‘", with: "'")
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,!?;:\"'“”()-— "))
    }

    private static let politeLeadInRegex: NSRegularExpression = {
        let prefixes = [
            #"can\s+you\s+please"#,
            #"could\s+you\s+please"#,
            #"would\s+you\s+please"#,
            #"will\s+you\s+please"#,
            #"i['’]d\s+like\s+you\s+to"#,
            #"i\s+would\s+like\s+you\s+to"#,
            #"i\s+want\s+you\s+to"#,
            #"i\s+need\s+you\s+to"#,
            #"go\s+ahead\s+and"#,
            #"can\s+you"#,
            #"could\s+you"#,
            #"would\s+you"#,
            #"will\s+you"#,
            #"please"#,
            #"okay"#,
            #"ok"#,
            #"hey"#,
            #"hi"#,
            #"so"#,
        ].joined(separator: "|")
        return try! NSRegularExpression(
            pattern: #"^(?:"# + prefixes + #")(?:[\s,.:;!?\-—]+|$)"#,
            options: [.caseInsensitive]
        )
    }()

    private static let trailingPleaseRegex = try! NSRegularExpression(
        pattern: #"(?:^|[\s,]+)please[\s.,!?]*$"#,
        options: [.caseInsensitive]
    )

    static func stripPoliteLeadIn(_ text: String) -> String? {
        var current = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !current.isEmpty else { return nil }
        var didStrip = false

        for _ in 0..<2 {
            let nsRange = NSRange(current.startIndex..., in: current)
            guard let match = politeLeadInRegex.firstMatch(in: current, range: nsRange),
                  match.range.location == 0,
                  let swiftRange = Range(match.range, in: current) else {
                break
            }
            current = String(current[swiftRange.upperBound...])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            didStrip = true
            if current.isEmpty { break }
        }

        if !current.isEmpty {
            let nsRange = NSRange(current.startIndex..., in: current)
            if let match = trailingPleaseRegex.firstMatch(in: current, range: nsRange),
               let swiftRange = Range(match.range, in: current) {
                current = String(current[..<swiftRange.lowerBound])
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                didStrip = true
            }
        }

        guard didStrip else { return nil }
        while let last = current.last, " .,!?;-—".contains(last) {
            current.removeLast()
        }
        current = current.trimmingCharacters(in: .whitespacesAndNewlines)
        return current.isEmpty ? nil : current
    }

    func handle(_ event: Transcriber.Event) {
        let text: String, latency: Int?
        switch event {
        case .volatile(let words):
            if prompts.active {
                if WakeGate.strip(words) == nil { prompts.hearing(words) }
                return
            }
            let now = Date()
            let trimmed = words.trimmingCharacters(in: .whitespacesAndNewlines)
            let inJoatWindow = now < conversationUntil || now < speechLatchedUntil
                || now < nameOnlyUntil || now < nameOnlyLatchedUntil
                || (now < followUpUntil && followUp != nil)
            let addressedVolatile = !trimmed.isEmpty && WakeGate.strip(trimmed) != nil
            if !inJoatWindow && !addressedVolatile && quickNotes.wantsSpeech(audioEnd: now) {
                quickNotes.hearing(words)
                return
            }
            if !inJoatWindow && !addressedVolatile {
                quickNotes.hearing(words)
            }
            // Latch active listening windows as soon as non-filler speech begins so
            // sentences that finish after the countdown still complete cleanly,
            // without ever mounting premature volatile `assistant.conversation` cards.
            guard !confirm.hasPending, !voice.wasSpeaking(at: now) else { return }
            guard !trimmed.isEmpty, !Self.isFiller(trimmed) else { return }
            if now < conversationUntil || now < speechLatchedUntil {
                speechLatchedUntil = max(speechLatchedUntil, now.addingTimeInterval(12))
            }
            if now < nameOnlyUntil || now < nameOnlyLatchedUntil {
                nameOnlyLatchedUntil = max(nameOnlyLatchedUntil, now.addingTimeInterval(12))
            }
            if now < followUpUntil, followUp != nil {
                followUpUntil = max(followUpUntil, now.addingTimeInterval(10))
            }
            return

        case .final(let words, let ms):
            text = words
            latency = ms
        }

        let now = Date()
        let spokeUntil = now.addingTimeInterval(-Double(latency ?? 0) / 1000)

        if prompts.active {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice kept out of the answer: \(text.prefix(80))")
                return
            }
            if prompts.heard(text, spokeUntil: spokeUntil) { return }
        }

        let inNameOnly = (now < nameOnlyUntil || now < nameOnlyLatchedUntil)
        let inFollowUp = (now < conversationUntil || now < speechLatchedUntil)
        let inAskedFollowUp = (now < followUpUntil && followUp != nil)

        let preAddressed = WakeGate.strip(text)
        let escapesRecordingNote = (preAddressed != nil) || inNameOnly || inFollowUp || inAskedFollowUp
        if !escapesRecordingNote, quickNotes.wantsSpeech(audioEnd: spokeUntil) {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice kept out of the note: \(text.prefix(80))")
                return
            }
            Log.add("quicknote heard (\(text.count) chars)")
            quickNotes.heard(text, audioEnd: spokeUntil)
            return
        }

        if confirm.hasPending {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored during confirmation: \(text.prefix(80))")
                return
            }
            let candidate = WakeGate.strip(text).flatMap { $0.isEmpty ? nil : $0 } ?? text
            let normalized = Self.normalizeConfirmSpeech(candidate)
            if Self.confirmCancelPhrases.contains(normalized) {
                Log.add("voice cancelled active confirmation: \(normalized)")
                confirm.declineActive(reason: "voice cancel")
                return
            }
            if Self.confirmAcceptPhrases.contains(normalized) {
                Log.add("voice attempted confirmation; reminding to tap touchpad")
                link.send(["kind": "hud.text", "containerId": 1, "text": "tap touchpad to confirm"])
                voice.say("Tap the glasses touchpad to confirm.")
                return
            }
            Log.add("speech ignored while confirmation pending: \(text.prefix(80))")
            return
        }

        if now < followUpUntil, let answer = followUp {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            if Self.isFiller(text) { return }
            followUp = nil
            followUpUntil = .distantPast
            let reply = WakeGate.strip(text).flatMap { $0.isEmpty ? nil : $0 } ?? text
            prepareConversationSession(now: now)
            lastAddressedAt = now
            appendModelHistory(role: "user", text: reply)
            publish(user: reply, assistant: nil, state: "answering")
            Log.add("follow-up answered: \(reply.prefix(100))")
            answer(reply)
            return
        }

        if navigation.active {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            let addressed = WakeGate.strip(text)
            if let request = addressed, !request.isEmpty, !Navigation.isCancel(request),
               let reply = capabilities.handle(request) {
                conversationVisible = false
                if !reply.suppressesHudReceipt {
                    hud.receipt(reply.text)
                }
                voice.say(reply.text)
                return
            }
            let joatListening = (inNameOnly || inFollowUp) && !Navigation.isCancel(text)
            if !joatListening, navigation.hear(text, addressed: addressed) {
                return
            }
        }

        var query = WakeGate.strip(text)
        if query != nil, voice.wasSpeaking(at: spokeUntil) {
            Log.add("own voice ignored: \(text.prefix(80))")
            return
        }

        if query == nil, let replyChannel = arbitrateUnaddressedReply(text, now: now) {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            let label = (replyChannel === mail) ? "email" : "text"
            Log.add("unaddressed reply routed to the \(label) thread on the lens")
            currentRequest = text
            defer { currentRequest = nil }
            if let reply = replyChannel.handle(text), !reply.text.isEmpty {
                prepareConversationSession(now: now)
                publish(user: text, assistant: reply.text, state: "ready")
                voice.say(reply.text)
            } else {
                conversationVisible = false
            }
            return
        }

        // Filler words ("um", "uh") inside an active listening window must never
        // consume the window or fire a bogus empty turn.
        if query == nil, Self.isFiller(text), (inNameOnly || inFollowUp) {
            return
        }

        if query == nil, inNameOnly, !Self.isFiller(text) {
            // Only a literal single-word "yes" could echo JOAT's "Yes?" prompt.
            let norm = Self.normalizeConfirmSpeech(text)
            if norm == "yes", voice.wasSpeaking(at: spokeUntil) {
                Log.add("own 'Yes?' echo ignored")
                return
            }
            query = text
        }

        if query == nil, inFollowUp, !Self.isFiller(text) {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            Log.add("follow-up heard inside the conversation window")
            query = text
        }

        guard let request = query else {
            speechLatchedUntil = .distantPast
            nameOnlyLatchedUntil = .distantPast
            Log.add("not addressed (ignored): \(text.prefix(80))")
            return
        }

        nameOnlyUntil = .distantPast
        nameOnlyLatchedUntil = .distantPast
        speechLatchedUntil = .distantPast

        prepareConversationSession(now: now)
        lastAddressedAt = now

        if request.isEmpty {
            nameOnlyUntil = now.addingTimeInterval(8)
            publish(user: text, assistant: "Yes?", state: "ready")
            voice.say("Yes?")
            return
        }

        // 1. Deterministic command chain first (0 ms, zero network).
        currentRequest = request
        defer { currentRequest = nil }
        if let reply = routeCommand(request) {
            if reply.text.isEmpty {
                if followUp == nil {
                    conversationVisible = false
                }
                return
            }
            Log.add("command answered: \(reply.text.prefix(100))")
            if reply.activated {
                conversationVisible = false
                if !reply.suppressesHudReceipt {
                    hud.receipt(reply.text)
                }
                voice.say(reply.text)
            } else {
                publishAnsweredCommand(user: request, assistant: reply.text)
            }
            return
        }

        // 2. Fast local on-phone queries (time, date, calendar agenda: <25 ms, zero LLM wait).
        if handleLocalFastQuery(request) {
            return
        }

        // 3. Streamed assistant turn via OnPhoneAI (Gemini / Claude / Mac SSE).
        appendModelHistory(role: "user", text: request)
        publish(user: request, assistant: nil, state: "answering")
        streamEpoch += 1
        let myEpoch = streamEpoch
        preDismissedEpoch = nil
        streamingTurnIndex = nil
        let promptTurns = modelHistory

        OnPhoneAI.shared.streamChat(
            turns: promptTurns,
            area: navigation.cachedArea(),
            lensContext: activeGlassesContextSummary(),
            macLink: macLink,
            search: search,
            toolBelt: toolBelt,
            onDelta: { [weak self] partial in
                guard let self, self.streamEpoch == myEpoch, !partial.isEmpty else { return }
                if self.preDismissedEpoch == myEpoch {
                    self.preDismissedEpoch = nil
                    self.turns.append(["role": "user", "text": String(request.prefix(3900))])
                    if self.turns.count > 16 {
                        self.turns.removeFirst(self.turns.count - 16)
                    }
                }
                self.publishStreamingDelta(partial)
            },
            completion: { [weak self] result in
                guard let self, self.streamEpoch == myEpoch else { return }
                let wasPreDismissed = (self.preDismissedEpoch == myEpoch)
                self.preDismissedEpoch = nil
                switch result {
                case .success(.claimedLens(let receipt)):
                    if !wasPreDismissed {
                        self.dismissActiveStreamingConversation()
                        self.preDismissedEpoch = nil
                    }
                    if let receipt, !receipt.isEmpty {
                        self.voice.say(receipt)
                    }
                case .success(.answer(let text)):
                    if wasPreDismissed {
                        self.turns.append(["role": "user", "text": String(request.prefix(3900))])
                        if self.turns.count > 16 {
                            self.turns.removeFirst(self.turns.count - 16)
                        }
                    }
                    self.appendModelHistory(role: "assistant", text: text)
                    self.finishStreamingTurn(text, armFollowUp: true)
                    if self.followUpWindowOpen { self.extendWindowAfterSpeech = true }
                    self.voice.say(text)
                case .failure(let failure):
                    if wasPreDismissed {
                        self.turns.append(["role": "user", "text": String(request.prefix(3900))])
                        if self.turns.count > 16 {
                            self.turns.removeFirst(self.turns.count - 16)
                        }
                    }
                    let finalAnswer = MacLink.explain(failure)
                    self.appendModelHistory(role: "assistant", text: finalAnswer)
                    self.finishStreamingTurn(finalAnswer, armFollowUp: true)
                    if self.followUpWindowOpen { self.extendWindowAfterSpeech = true }
                    self.voice.say(finalAnswer)
                }
            }
        )
    }

    private func publishAnsweredCommand(user: String, assistant: String) {
        prepareConversationSession(now: Date())
        lastAddressedAt = Date()
        appendModelHistory(role: "user", text: user)
        appendModelHistory(role: "assistant", text: assistant)
        let shouldListen = scheduleFollowUpTimer()
        publish(user: user, assistant: assistant, state: "ready", listening: shouldListen)
        if shouldListen { extendWindowAfterSpeech = true }
        voice.say(assistant)
    }

    // MARK: - Sub-25ms Local Fast Queries (Time, Date, Calendar)

    private static let timeQueryRegex = try! NSRegularExpression(
        pattern: #"^(?:what(?:'s|\s+is)?\s+the\s+(?:current\s+)?time|what\s+time\s+is\s+it|tell\s+me\s+the\s+time|do\s+you\s+have\s+the\s+time)(?:\s+right\s+now)?$"#,
        options: [.caseInsensitive]
    )

    private static let dateQueryRegex = try! NSRegularExpression(
        pattern: #"^(?:what(?:'s|\s+is)?\s+(?:today(?:'s)?\s+date|the\s+date(?:\s+today)?)|what\s+day\s+is\s+(?:it|today))$"#,
        options: [.caseInsensitive]
    )

    private static let calendarFastRegex = try! NSRegularExpression(
        pattern: #"^(?:what(?:'s|\s+is|\s+do\s+i\s+have)?\s+on\s+my\s+(?:calendar|schedule)|what(?:'s|\s+is)\s+my\s+schedule(?:\s+like)?|do\s+i\s+have\s+(?:any\s+)?(?:meetings|events|appointments)|check\s+my\s+(?:calendar|schedule))(?:\s+(?:for\s+)?(?<spanWord>today|tomorrow|this\s+week))?$"#,
        options: [.caseInsensitive]
    )

    private func handleLocalFastQuery(_ request: String) -> Bool {
        let stripped = Self.stripPoliteLeadIn(request) ?? request
        let clean = stripped
            .replacingOccurrences(of: "’", with: "'")
            .trimmingCharacters(in: CharacterSet(charactersIn: " .,!?;:"))
        guard !clean.isEmpty else { return false }
        let nsRange = NSRange(clean.startIndex..., in: clean)

        if Self.timeQueryRegex.firstMatch(in: clean, range: nsRange) != nil {
            let now = Date()
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "h:mm a"
            publishAnsweredCommand(user: request, assistant: "It is \(f.string(from: now)).")
            return true
        }

        if Self.dateQueryRegex.firstMatch(in: clean, range: nsRange) != nil {
            let now = Date()
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "EEEE, MMMM d, yyyy"
            publishAnsweredCommand(user: request, assistant: "Today is \(f.string(from: now)).")
            return true
        }

        if let match = Self.calendarFastRegex.firstMatch(in: clean, range: nsRange) {
            let spanRaw: String
            let spanRange = match.range(withName: "spanWord")
            if spanRange.location != NSNotFound, let r = Range(spanRange, in: clean) {
                spanRaw = String(clean[r]).lowercased()
            } else {
                spanRaw = "today"
            }
            Task { @MainActor [weak self] in
                guard let self else { return }
                let answer = await self.localCalendarSummary(span: spanRaw)
                self.publishAnsweredCommand(user: request, assistant: answer)
            }
            return true
        }

        return false
    }

    @MainActor
    private func localCalendarSummary(span: String) async -> String {
        let cal = Calendar.current
        let today = cal.startOfDay(for: Date())
        let startDay: Date
        let days: Int
        let label: String
        if span == "tomorrow" {
            startDay = cal.date(byAdding: .day, value: 1, to: today) ?? today
            days = 1
            label = "tomorrow"
        } else if span.contains("week") {
            startDay = today
            days = 7
            label = "this week"
        } else {
            startDay = today
            days = 1
            label = "today"
        }

        let ekStore = reminders.store.store
        let eventsAllowed = EKEventStore.authorizationStatus(for: .event) == .fullAccess
        let remindersAllowed = reminders.store.authorized
        guard eventsAllowed || remindersAllowed else {
            return "Allow Calendars and Reminders for JOAT Brain in iPhone Settings > Privacy & Security first."
        }

        let endDay = cal.date(byAdding: .day, value: days, to: startDay) ?? startDay
        let events: [EKEvent] = eventsAllowed
            ? ekStore.events(matching: ekStore.predicateForEvents(withStart: startDay, end: endDay, calendars: nil))
            : []
        let datedReminders: [EKReminder] = remindersAllowed
            ? await reminders.store.incomplete(dueFrom: startDay, until: endDay)
            : []

        let tf = DateFormatter()
        tf.locale = Locale(identifier: "en_US_POSIX")
        tf.timeZone = cal.timeZone
        tf.dateFormat = "h:mm a"

        var items: [(date: Date, text: String)] = []
        for ev in events {
            guard let s = ev.startDate, let e = ev.endDate else { continue }
            let overlaps = ev.isAllDay ? (cal.startOfDay(for: s) < endDay && e >= startDay) : (s < endDay && e > startDay)
            guard overlaps else { continue }
            let title = (ev.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let cleanTitle = title.isEmpty ? "Untitled event" : title
            items.append((s, ev.isAllDay ? "\(cleanTitle) (all day)" : "\(tf.string(from: s)) \(cleanTitle)"))
        }
        for rem in datedReminders {
            guard let due = rem.dueDate, due >= startDay && due < endDay else { continue }
            let title = (rem.title ?? "Reminder").trimmingCharacters(in: .whitespacesAndNewlines)
            let timed = rem.dueDateComponents?.hour != nil
            items.append((due, timed ? "\(tf.string(from: due)) \(title)" : "\(title) (due)"))
        }

        items.sort { $0.date < $1.date }
        if items.isEmpty {
            return "You have nothing scheduled \(label)."
        }
        let preview = items.prefix(4).map(\.text).joined(separator: "; ")
        let more = items.count > 4 ? " (+\(items.count - 4) more)" : ""
        return "You have \(items.count) item\(items.count == 1 ? "" : "s") \(label): \(preview)\(more)."
    }

    func routeCommand(_ request: String) -> CommandReply? {
        for command in orderedCommands(for: request) {
            if let reply = command.handle(request) {
                return reply
            }
        }
        if let stripped = Self.stripPoliteLeadIn(request), !stripped.isEmpty {
            for command in orderedCommands(for: stripped) {
                if let reply = command.handle(stripped) {
                    return reply
                }
            }
        }
        return nil
    }

    // MARK: - Reply Arbitration

    private static func isGenericReplyUtterance(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if Messages.isAIReplyRequest(trimmed) { return false }
        let norm = Grammar.normalize(trimmed)
        if norm.contains("email") || norm.contains("mail") { return false }
        return Grammar.match(Messages.replyRe, trimmed) != nil
            || Grammar.match(Messages.bareReplyRe, norm) != nil
    }

    func activeGlassesContextSummary() -> String? {
        switch capabilities.activeCapability {
        case "MESSAGES":
            if let sender = messages.threadOnGlass, !sender.isEmpty {
                return "The wearer currently has the Messages conversation with \(sender) open on the glasses."
            }
            return "The wearer currently has the Messages thread list open on the glasses."
        case "MAIL":
            if let summary = mail.activeGlassThreadSummary() {
                return "The wearer currently has the Mail thread '\(summary.subject)' with \(summary.participantLabel) open on the glasses."
            }
            return "The wearer currently has the Mail inbox list open on the glasses."
        case "QUICKNOTES":
            return quickNotes.active
                ? "The wearer currently has an open QuickNote on the glasses."
                : "The wearer currently has QuickNotes open on the glasses."
        case "AGENT TERMINAL":
            return "The wearer currently has Mac Agent Terminal open on the glasses."
        case "TASKS", "PLANNING", "CALENDAR":
            return "The wearer currently has Reminders open on the glasses."
        case "NOTIFICATIONS":
            return "The wearer currently has Notifications open on the glasses."
        case "ACTIVITY":
            return "The wearer currently has Activity open on the glasses."
        case "NAVIGATION":
            return "The wearer currently has Navigation open on the glasses."
        default:
            if navigation.active {
                return "The wearer currently has active turn-by-turn Navigation on the glasses."
            }
            if quickNotes.active {
                return "The wearer currently has an active QuickNote recording."
            }
            return nil
        }
    }

    func arbitrateUnaddressedReply(_ text: String, now: Date) -> VoiceCommand? {
        let isGeneric = Self.isGenericReplyUtterance(text)
        if capabilities.activeCapability == "MAIL", mail.threadOnGlass != nil {
            if mail.takesUnaddressed(text, now: now) || isGeneric { return mail }
        } else if capabilities.activeCapability == "MESSAGES", messages.threadOnGlass != nil {
            if messages.takesUnaddressed(text, now: now) || isGeneric { return messages }
        }
        return nil
    }

    private func orderedCommands(for request: String) -> [VoiceCommand] {
        guard Self.isGenericReplyUtterance(request) else { return commands }
        let preferMessages: Bool
        if capabilities.activeCapability == "MESSAGES" {
            preferMessages = true
        } else if capabilities.activeCapability == "MAIL" {
            preferMessages = false
        } else {
            preferMessages = messages.lastArrival > mail.lastArrival
        }
        guard preferMessages,
              let mailIdx = commands.firstIndex(where: { $0 === mail }),
              let msgIdx = commands.firstIndex(where: { $0 === messages }),
              mailIdx < msgIdx else {
            return commands
        }
        var reordered = commands
        reordered.swapAt(mailIdx, msgIdx)
        return reordered
    }

    static let fillerWords: Set<String> = [
        "the", "a", "an", "uh", "um", "umm", "hmm", "mm", "mhm", "ah", "oh",
        "you", "thank", "thanks", "for", "watching", "bye", "so",
    ]

    static func isFiller(_ text: String) -> Bool {
        let words = text.lowercased().split(whereSeparator: { !($0.isLetter || $0 == "'") }).map(String.init)
        return words.isEmpty || words.allSatisfy(fillerWords.contains)
    }

    /// The conversation page's Continue / Exit (assistant-conversation input).
    /// Tapping to exit retires `conversationId` in Even Hub (`this.retired.add`),
    /// so we MUST immediately rotate to a fresh `conversationId` while keeping
    /// `modelHistory` for 30 s of conversational context!
    func conversationChoice(_ action: String) {
        if action == "ptt_start" {
            nameOnlyUntil = Date().addingTimeInterval(15)
            nameOnlyLatchedUntil = nameOnlyUntil
            Log.add("on-demand push-to-talk started")
            return
        }
        if action == "ptt_end" {
            nameOnlyUntil = Date().addingTimeInterval(4)
            Log.add("on-demand push-to-talk released")
            return
        }
        if action == "exit" {
            conversationUntil = .distantPast
            speechLatchedUntil = .distantPast
            rotateCardConversation()
            if BleDiagnostics.currentMicModeRaw() == "on_demand" {
                link.send(["kind": "audio.control", "open": false])
            }
        } else if action == "continue" {
            let open = scheduleFollowUpTimer()
            if open, conversationVisible, !turns.isEmpty {
                sendSnapshot(state: "ready", listening: true)
            }
        }
        Log.add("conversation \(action)")
    }

    // MARK: - Conversation Wire Publishing

    var currentConversationId: String { conversationId }
    var currentRevision: Int { revision }
    var currentTurns: [[String: String]] { turns }

    @MainActor
    func beginStreamingConversationForTesting(user: String, partialAssistant: String? = nil) {
        prepareConversationSession(now: Date())
        lastAddressedAt = Date()
        streamEpoch += 1
        preDismissedEpoch = nil
        publish(user: user, assistant: nil, state: "answering")
        streamingTurnIndex = nil
        if let partialAssistant {
            publishStreamingDelta(partialAssistant)
        }
    }

    @MainActor
    func dismissActiveStreamingConversation() {
        preDismissedEpoch = streamEpoch
        if conversationVisible {
            let dismissedId = conversationId
            let dismissedRev = revision
            link.send([
                "kind": "assistant.conversation",
                "operation": "dismiss",
                "conversationId": dismissedId,
                "revision": dismissedRev,
            ])
            Log.add("tx assistant.conversation rev=\(dismissedRev) op=dismiss")
        }
        // Always rotate `conversationId` after a dismiss because Even Hub adds
        // the dismissed `conversationId` to `AssistantConversationSurface.retired`.
        rotateCardConversation()
    }

    @discardableResult
    private func scheduleFollowUpTimer() -> Bool {
        cueExpiry?.cancel()
        cueExpiry = nil
        let window = Self.conversationWindow
        guard window > 0 else {
            conversationUntil = .distantPast
            return false
        }
        conversationUntil = Date().addingTimeInterval(window)
        Log.add("follow-up window open for \(Int(window)) s")
        scheduleCueExpiry(after: window)
        return true
    }

    private func scheduleCueExpiry(after seconds: TimeInterval) {
        cueExpiry?.cancel()
        let expectedCid = conversationId
        let expiry = DispatchWorkItem { [weak self] in
            guard let self,
                  self.conversationVisible,
                  self.conversationId == expectedCid,
                  !self.turns.isEmpty,
                  !self.followUpWindowOpen else { return }
            self.sendSnapshot(state: "ready", listening: false)
            if BleDiagnostics.currentMicModeRaw() == "on_demand" {
                self.link.send(["kind": "audio.control", "open": false])
            }
            Log.add("follow-up window closed")
        }
        cueExpiry = expiry
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds + 0.02, execute: expiry)
    }

    private func refreshFollowUpAfterSpeechIfNeeded() {
        guard extendWindowAfterSpeech else { return }
        extendWindowAfterSpeech = false
        let window = Self.conversationWindow
        guard window > 0, conversationVisible else { return }
        conversationUntil = Date().addingTimeInterval(window)
        scheduleCueExpiry(after: window)
    }

    private func sendSnapshot(state: String, listening: Bool) {
        guard !turns.isEmpty else { return }
        revision += 1
        conversationVisible = true
        var wire: [String: Any] = [
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": conversationId,
            "revision": revision,
            "state": state,
            "turns": turns,
            "continueAfterMs": 30_000,
            "earlierTurnsOmitted": false,
            "listening": listening,
        ]
        if !listening && state != "ready" {
            wire.removeValue(forKey: "listening")
        }
        link.send(wire)
        Log.add("tx assistant.conversation rev=\(revision) state=\(state) turns=\(turns.count)\(listening ? " +listening" : "")")
    }

    private func publishStreamingDelta(_ partial: String) {
        let clipped = String(partial.prefix(3900))
        guard !clipped.isEmpty else { return }
        if let idx = streamingTurnIndex,
           turns.indices.contains(idx),
           turns[idx]["role"] == "assistant" {
            if turns[idx]["text"] == clipped { return }
            turns[idx]["text"] = clipped
        } else {
            turns.append(["role": "assistant", "text": clipped])
            if turns.count > 16 {
                turns.removeFirst(turns.count - 16)
            }
            streamingTurnIndex = turns.count - 1
        }
        revision += 1
        conversationVisible = true
        link.send([
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": conversationId,
            "revision": revision,
            "state": "answering",
            "turns": turns,
            "continueAfterMs": 30_000,
            "earlierTurnsOmitted": false,
        ])
    }

    private func finishStreamingTurn(_ finalAnswer: String, armFollowUp: Bool = false) {
        let clipped = String((finalAnswer.isEmpty ? "…" : finalAnswer).prefix(3900))
        if let idx = streamingTurnIndex,
           turns.indices.contains(idx),
           turns[idx]["role"] == "assistant" {
            turns[idx]["text"] = clipped
        } else {
            turns.append(["role": "assistant", "text": clipped])
            if turns.count > 16 {
                turns.removeFirst(turns.count - 16)
            }
        }
        streamingTurnIndex = nil
        let shouldListen = armFollowUp ? scheduleFollowUpTimer() : false
        sendSnapshot(state: "ready", listening: shouldListen)
    }

    private func publish(user: String?, assistant: String?, state: String, listening: Bool = false) {
        streamingTurnIndex = nil
        if let user, !user.isEmpty { turns.append(["role": "user", "text": String(user.prefix(3900))]) }
        if let assistant, !assistant.isEmpty { turns.append(["role": "assistant", "text": String(assistant.prefix(3900))]) }
        if turns.count > 16 { turns.removeFirst(turns.count - 16) }
        guard !turns.isEmpty else { return }
        sendSnapshot(state: state, listening: listening)
    }
}
