import Foundation

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
    private var pendingConversationId: String?
    private var revision = 0
    private var turns: [[String: String]] = []
    private var lastAddressedAt = Date.distantPast
    private var lastVolatileBroadcastAt = Date.distantPast
    private var lastVolatilePreview = ""
    private var activeStreamId = UUID()
    private var dismissedStreamId: UUID?
    private var streamingTurnIndex: Int?
    private(set) var toolBelt: PhoneToolBelt!
    /// After the name alone ("JOAT" -> "Yes?"), the next utterance is the request.
    private var nameOnlyUntil = Date.distantPast
    /// orchestrator.py ConversationWindow: after JOAT answers, the next
    /// question needs no name for a while; every answer re-opens it, the
    /// conversation page's Exit closes it.
    /// How long, in seconds: the wearer sets it in JOAT Brain ("Follow-up
    /// without the name"); 0 is off, 8 unless changed.
    static let followUpKey = "joat.followUpSeconds"
    static var conversationWindow: TimeInterval {
        UserDefaults.standard.object(forKey: followUpKey) as? Double ?? 8
    }
    private var conversationUntil = Date.distantPast

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
    init(server: LoopbackServer, stt: Transcriber, shadow: Transcriber?, voice: Voice, linkOverride: PluginLink? = nil) {
        let activeLink: PluginLink = linkOverride ?? server
        let hasTestLink = linkOverride != nil
        self.server = server
        self.link = activeLink
        hud = Hud(link: activeLink)
        panels = PanelPublisher(link: activeLink)
        confirm = ConfirmGate(link: activeLink)
        reminders = Reminders(link: activeLink, hud: hud, confirm: confirm)
        cards = Cards(link: activeLink)
        quickNotes = QuickNotes(link: activeLink, hud: hud)
        navigation = Navigation(link: activeLink)
        messages = Messages(hud: hud, confirm: confirm, cards: cards)
        mail = Mail(hud: hud, confirm: confirm, cards: cards)
        prompts = Prompts(link: activeLink)
        capabilities = CapabilityMenu(link: activeLink, panels: panels,
                                      reminders: reminders,
                                      cards: cards, quickNotes: quickNotes)
        quickNotes.menu = capabilities
        capabilities.navigation = navigation
        capabilities.messages = messages
        capabilities.mail = mail
        messages.onChange = { [capabilities] in capabilities.refreshMessages() }
        messages.onThreadChange = { [capabilities] in capabilities.refreshThread() }
        messages.link = activeLink
        messages.prompts = prompts
        // Texts through the home Mac, when this build carries its address
        // (MacMessages.swift): the glasses stay on this phone alone.
        messages.mac = macMessages
        mail.link = activeLink
        mail.prompts = prompts
        mail.say = { [voice] text in voice.say(text) }
        mail.onChange = { [capabilities] in capabilities.refreshMail() }
        mail.onThreadChange = { [capabilities] in capabilities.refreshMailThread() }
        router.register(prompts)
        server.onUplink = { [router] kind, obj, raw in router.route(kind: kind, message: obj, raw: raw) }
        server.onPluginConnected = { [router, pomodoro] in
            router.pluginConnected()
            DispatchQueue.main.async { pomodoro.flushRecoveryCard() }
        }
        // Pomodoro + countdown timer (services/pomodoro.py, panel_commands.py).
        panels.register(pomodoro)
        pomodoro.onChange = { [panels] in panels.publish("pomodoro") }
        pomodoro.onCard = { [cards] source, summary in cards.post(source: source, channel: "Pomodoro", summary: summary) }
        router.register(panels)
        // "Open my notes", "show planning", "open mail": surface opens come first (commands.py).
        commands.append(capabilities)
        // "Email Sarah saying …", "Reply …", "Message Mom saying …".
        commands.append(mail)
        commands.append(messages)
        messages.say = { [voice] text in voice.say(text) }
        commands.append(SpokenRepliesCommand(voice: voice))
        // "Navigate to the library": the place, then the mode, then the route.
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
        // News: the rail tab, every row read in full first (services/news.py).
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
        // QuickNotes (voice_note_capture.py, note_commands.py) and its rail tab.
        // Placed AHEAD of PomodoroCommands so an open voice note's "pause"/"resume"
        // is never stolen by a background Pomodoro timer.
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
        // Reminders: the glasses' confirm gate, EventKit, the Planner tab and
        // the due page (productivity_actions.py, planner_panel.py, task_monitor.py).
        router.register(confirm)
        router.register(MicResume(link: activeLink))
        // Texts and emails wait for the glasses: off, in the case or disconnected,
        // arrivals are held and shown when they come back (MessageHold).
        router.register(MessageHold(channels: [messages, mail], connected: { [server] in hasTestLink || server.clients > 0 }))
        router.register(reminders)
        router.register(capabilities)
        router.register(cards)
        panels.register(reminders)
        reminders.publishPanel = { [panels] in panels.publish("planner") }
        reminders.say = { [voice] text in voice.say(text) }
        commands.append(reminders)
        self.stt = stt
        self.shadow = shadow
        self.voice = voice
        server.onAudio = { [weak stt, weak shadow, quickNotes] samples in
            stt?.feed(samples)
            shadow?.feed(samples)
            DispatchQueue.main.async { quickNotes.audioFrame(samples) }
        }
        // Features keep their state on the main queue.
        stt.onEvent = { [weak self] event in DispatchQueue.main.async { self?.handle(event) } }
        reminders.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
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
        // Last, once every member is set: the poll delivers into `messages`.
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
        // Notes saved while no model answered get summarized once one does.
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [quickNotes] in quickNotes.retryPendingInsights() }
        insightsTimer = Timer.scheduledTimer(withTimeInterval: 600, repeats: true) { [quickNotes] _ in
            quickNotes.retryPendingInsights()
        }
    }

    /// Ask, show and say a question; the next sentence is its answer.
    private func ask(_ question: String, answer: @escaping (String) -> Void) {
        followUp = answer
        // Long enough to hear the question and then say the answer.
        followUpUntil = Date().addingTimeInterval(15)
        nameOnlyUntil = .distantPast
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

    /// Strips polite conversational lead-ins (up to 2 prefixes iteratively) and
    /// trailing `"please"` (`", please"` / `" please"`), preserving the command
    /// body's original casing. Returns `nil` when nothing was stripped or when
    /// stripping leaves an empty utterance.
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
            if prompts.active { prompts.hearing(words); return }
            let now = Date()
            if quickNotes.wantsSpeech(audioEnd: now) {
                quickNotes.hearing(words)
                return
            }
            quickNotes.hearing(words)
            guard !confirm.hasPending, !navigation.active, !voice.wasSpeaking(at: now) else { return }
            let trimmed = words.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return }
            let preview: String?
            if let stripped = WakeGate.strip(trimmed) {
                let clean = stripped.trimmingCharacters(in: .whitespacesAndNewlines)
                preview = clean.isEmpty ? nil : clean
            } else if now < nameOnlyUntil {
                preview = trimmed
            } else if now < conversationUntil, !Self.isFiller(trimmed) {
                preview = trimmed
            } else {
                preview = nil
            }
            guard let preview,
                  preview != lastVolatilePreview,
                  now.timeIntervalSince(lastVolatileBroadcastAt) >= 0.12 else { return }
            lastVolatileBroadcastAt = now
            lastVolatilePreview = preview
            publishVolatilePreview(preview, now: now)
            return

        case .final(let words, let ms):
            text = words
            latency = ms
            lastVolatilePreview = ""
        }

        let now = Date()
        // When the sentence ended. The phone's own reply, heard back by the
        // glasses mic, must never answer JOAT's question.
        let spokeUntil = now.addingTimeInterval(-Double(latency ?? 0) / 1000)

        // A question on the glasses (a reply's words, a recipient): the next
        // sentence is its answer, never a command or the note's.
        if prompts.active {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice kept out of the answer: \(text.prefix(80))")
                return
            }
            if prompts.heard(text) { return }
        }

        // An open note's lane: the sentence is the note's, never a command
        // (the Mac's exclusive capture lane).
        if quickNotes.wantsSpeech(audioEnd: spokeUntil) {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice kept out of the note: \(text.prefix(80))")
                return
            }
            Log.add("quicknote heard (\(text.count) chars)")
            quickNotes.heard(text, audioEnd: spokeUntil)
            return
        }

        // Active hardware confirmation gate: spoken "cancel"/"no" declines immediately;
        // spoken "yes"/"send" reminds to tap the touchpad; background speech never
        // clobbers the confirmation card with an assistant chat bubble.
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
            followUp = nil
            followUpUntil = .distantPast
            let reply = WakeGate.strip(text).flatMap { $0.isEmpty ? nil : $0 } ?? text
            lastAddressedAt = now
            publish(user: reply, assistant: nil, state: "answering")
            Log.add("follow-up answered: \(reply.prefix(100))")
            answer(reply)
            return
        }

        // A live route owns speech (session.py handle_transcript): its cancel
        // words, its spoken choices, and "open my notes"; nothing else
        // reaches a command or the assistant until it ends.
        if navigation.active {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            let addressed = WakeGate.strip(text)
            if let request = addressed, !request.isEmpty, !Navigation.isCancel(request),
               let reply = capabilities.handle(request) {
                if !reply.suppressesHudReceipt {
                    hud.receipt(reply.text)
                }
                voice.say(reply.text)
                return
            }
            navigation.hear(text, addressed: addressed)
            return
        }

        if now < nameOnlyUntil, voice.wasSpeaking(at: spokeUntil) {
            Log.add("own voice ignored: \(text.prefix(80))")
            return
        }

        var query = WakeGate.strip(text)
        if query != nil, voice.wasSpeaking(at: spokeUntil) {
            Log.add("own voice ignored: \(text.prefix(80))")
            return
        }

        // Unaddressed "reply …" arbitration across Mail and Messages.
        if query == nil, let replyChannel = arbitrateUnaddressedReply(text, now: now) {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            let label = (replyChannel === mail) ? "email" : "text"
            Log.add("unaddressed reply routed to newest \(label)")
            currentRequest = text
            defer { currentRequest = nil }
            if let reply = replyChannel.handle(text), !reply.text.isEmpty {
                publish(user: text, assistant: reply.text, state: "ready")
                voice.say(reply.text)
            }
            return
        }

        if query == nil, now < nameOnlyUntil { query = text }   // the follow-up after "JOAT"
        if query == nil, now < conversationUntil, !Self.isFiller(text) {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            Log.add("follow-up heard inside the conversation window")
            query = text
        }
        guard let request = query else {
            Log.add("not addressed (ignored): \(text.prefix(80))")
            return
        }
        nameOnlyUntil = .distantPast
        // A new conversation after 30 s of quiet, as on the Mac.
        if now.timeIntervalSince(lastAddressedAt) > 30 {
            if let pending = pendingConversationId {
                conversationId = pending
                pendingConversationId = nil
            } else {
                conversationId = "phone-\(UUID().uuidString.prefix(8).lowercased())"
                revision = 0
            }
            turns = []
        } else {
            pendingConversationId = nil
        }
        lastAddressedAt = now

        if request.isEmpty {
            nameOnlyUntil = now.addingTimeInterval(8)
            publish(user: text, assistant: "Yes?", state: "ready")
            voice.say("Yes?")
            return
        }

        // The command chain first, as the Mac's VoiceCommandRouter does.
        currentRequest = request
        defer { currentRequest = nil }
        if let reply = routeCommand(request) {
            // Empty: the command drives the lens itself (a confirmation,
            // a question) and speaks for itself.
            if reply.text.isEmpty { return }
            Log.add("command answered: \(reply.text.prefix(100))")
            if reply.activated {
                if !reply.suppressesHudReceipt {
                    hud.receipt(reply.text)
                }
            } else {
                publish(user: request, assistant: reply.text, state: "ready")
            }
            voice.say(reply.text)
            return
        }

        // Everything else streams through OnPhoneAI (Gemini / Claude / Mac SSE).
        publish(user: request, assistant: nil, state: "answering")
        let conversation = conversationId
        let streamId = UUID()
        activeStreamId = streamId
        dismissedStreamId = nil
        streamingTurnIndex = nil
        let promptTurns = turns

        OnPhoneAI.shared.streamChat(
            turns: promptTurns,
            area: navigation.cachedArea(),
            macLink: macLink,
            search: search,
            toolBelt: toolBelt,
            onDelta: { [weak self] partial in
                guard let self,
                      self.conversationId == conversation,
                      (self.activeStreamId == streamId || self.dismissedStreamId == streamId),
                      !partial.isEmpty else { return }
                if self.activeStreamId != streamId {
                    self.activeStreamId = streamId
                    self.dismissedStreamId = nil
                    self.turns.append(["role": "user", "text": String(request.prefix(3900))])
                    if self.turns.count > 16 {
                        self.turns.removeFirst(self.turns.count - 16)
                    }
                }
                self.publishStreamingDelta(partial)
            },
            completion: { [weak self] result in
                guard let self,
                      self.conversationId == conversation,
                      (self.activeStreamId == streamId || self.dismissedStreamId == streamId) else { return }
                let wasPreDismissed = (self.dismissedStreamId == streamId && self.activeStreamId != streamId)
                self.dismissedStreamId = nil
                switch result {
                case .success(.claimedLens(let receipt)):
                    if !wasPreDismissed {
                        self.dismissActiveStreamingConversation()
                        self.dismissedStreamId = nil
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
                    self.finishStreamingTurn(text)
                    self.voice.say(text)
                case .failure(let failure):
                    if wasPreDismissed {
                        self.turns.append(["role": "user", "text": String(request.prefix(3900))])
                        if self.turns.count > 16 {
                            self.turns.removeFirst(self.turns.count - 16)
                        }
                    }
                    let finalAnswer = MacLink.explain(failure)
                    self.finishStreamingTurn(finalAnswer)
                    self.voice.say(finalAnswer)
                }
            }
        )
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
        let norm = Grammar.normalize(trimmed)
        if norm.contains("email") || norm.contains("mail") { return false }
        return Grammar.match(Messages.replyRe, trimmed) != nil
            || Grammar.match(Messages.bareReplyRe, norm) != nil
    }

    private func arbitrateUnaddressedReply(_ text: String, now: Date) -> VoiceCommand? {
        let mailTakes = mail.takesUnaddressed(text, now: now)
        let msgTakes = messages.takesUnaddressed(text, now: now)
        let isGeneric = Self.isGenericReplyUtterance(text)

        if capabilities.activeCapability == "MAIL" {
            if mailTakes || (mail.threadOnGlass != nil && isGeneric) { return mail }
        } else if capabilities.activeCapability == "MESSAGES" {
            if msgTakes || (messages.threadOnGlass != nil && isGeneric) { return messages }
        }
        if mailTakes && msgTakes {
            return mail.lastArrival >= messages.lastArrival ? mail : messages
        }
        if mailTakes { return mail }
        if msgTakes { return messages }
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

    /// orchestrator.py _FILLER_WORDS: never a request on its own.
    static let fillerWords: Set<String> = [
        "the", "a", "an", "uh", "um", "umm", "hmm", "mm", "mhm", "ah", "oh",
        "you", "thank", "thanks", "for", "watching", "bye", "so",
    ]

    static func isFiller(_ text: String) -> Bool {
        let words = text.lowercased().split(whereSeparator: { !($0.isLetter || $0 == "'") }).map(String.init)
        return words.isEmpty || words.allSatisfy(fillerWords.contains)
    }

    /// The conversation page's Continue / Exit (assistant-conversation input).
    func conversationChoice(_ action: String) {
        if action == "exit" { conversationUntil = .distantPast }
        if action == "continue" { conversationUntil = Date().addingTimeInterval(Self.conversationWindow) }
        Log.add("conversation \(action)")
    }

    // MARK: - Conversation Wire Publishing

    var currentConversationId: String { conversationId }
    var currentRevision: Int { revision }
    var currentTurns: [[String: String]] { turns }

    @MainActor
    func beginStreamingConversationForTesting(user: String, partialAssistant: String? = nil) {
        publish(user: user, assistant: nil, state: "answering")
        let streamId = UUID()
        activeStreamId = streamId
        dismissedStreamId = nil
        streamingTurnIndex = nil
        if let partialAssistant {
            publishStreamingDelta(partialAssistant)
        }
    }

    @MainActor
    func dismissActiveStreamingConversation() {
        dismissedStreamId = activeStreamId
        activeStreamId = UUID()
        if let idx = streamingTurnIndex,
           turns.indices.contains(idx),
           turns[idx]["role"] == "assistant" {
            turns.remove(at: idx)
        }
        streamingTurnIndex = nil
        if turns.last?["role"] == "user" {
            turns.removeLast()
        }
        revision += 1
        link.send([
            "kind": "assistant.conversation",
            "operation": "dismiss",
            "conversationId": conversationId,
            "revision": revision,
        ])
        Log.add("tx assistant.conversation rev=\(revision) op=dismiss")
    }

    private func publishVolatilePreview(_ preview: String, now: Date) {
        let stale = now.timeIntervalSince(lastAddressedAt) > 30
        let cid: String
        let baseTurns: [[String: String]]
        if stale {
            if let existing = pendingConversationId {
                cid = existing
            } else {
                let fresh = "phone-\(UUID().uuidString.prefix(8).lowercased())"
                pendingConversationId = fresh
                revision = 0
                cid = fresh
            }
            baseTurns = []
        } else {
            cid = conversationId
            baseTurns = Array(turns.suffix(15))
        }
        let previewTurn: [String: String] = ["role": "user", "text": String(preview.prefix(3900)) + "…"]
        let previewTurns = baseTurns + [previewTurn]
        revision += 1
        link.send([
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": cid,
            "revision": revision,
            "state": "thinking",
            "turns": previewTurns,
            "continueAfterMs": 30_000,
            "earlierTurnsOmitted": false,
        ])
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

    private func finishStreamingTurn(_ finalAnswer: String) {
        conversationUntil = Date().addingTimeInterval(Self.conversationWindow)
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
        revision += 1
        link.send([
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": conversationId,
            "revision": revision,
            "state": "ready",
            "turns": turns,
            "continueAfterMs": 30_000,
            "earlierTurnsOmitted": false,
        ])
        Log.add("tx assistant.conversation rev=\(revision) state=ready turns=\(turns.count)")
    }

    private func publish(user: String?, assistant: String?, state: String) {
        activeStreamId = UUID()
        dismissedStreamId = nil
        streamingTurnIndex = nil
        // JOAT answered (or asked): keep listening without the name.
        if state == "ready", assistant != nil {
            conversationUntil = Date().addingTimeInterval(Self.conversationWindow)
        }
        if let user, !user.isEmpty { turns.append(["role": "user", "text": String(user.prefix(3900))]) }
        if let assistant, !assistant.isEmpty { turns.append(["role": "assistant", "text": String(assistant.prefix(3900))]) }
        if turns.count > 16 { turns.removeFirst(turns.count - 16) }
        guard !turns.isEmpty else { return }
        revision += 1
        link.send([
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": conversationId,
            "revision": revision,
            "state": state,
            "turns": turns,
            "continueAfterMs": 30_000,
            "earlierTurnsOmitted": false,
        ])
        Log.add("tx assistant.conversation rev=\(revision) state=\(state) turns=\(turns.count)")
    }
}
