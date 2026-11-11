import Foundation

/// The first brain loop on the phone: glasses mic → on-device STT → wake
/// gate → the glasses' native conversation page + a spoken reply.
///
/// Lab reply only: it repeats the request back, which proves the round trip
/// (speak, the phone hears it, the lens and the ear answer) before any model
/// is wired in. Unaddressed speech is transcribed for the gate and shown in
/// JOAT Brain's own log, never on the glasses or aloud — as on the Mac.
final class Brain {
    private let server: LoopbackServer
    private let stt: Transcriber
    /// Same audio, the other Apple engine: logged only, for the comparison.
    private let shadow: Transcriber?
    private let voice: Voice
    private var conversationId = "phone-\(UUID().uuidString.prefix(8).lowercased())"
    private var revision = 0
    private var turns: [[String: String]] = []
    private var lastAddressedAt = Date.distantPast
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
    /// Routes to a place with Apple Maps (services/navigation.py).
    let navigation: Navigation
    /// Texts in and out through Shortcuts automations (Messages.swift).
    let messages: Messages
    /// Questions the glasses ask for a feature (Messages' "What should I say?").
    let prompts: Prompts
    /// A question JOAT asked ("When should I remind you?"): the next
    /// sentence answers it, no name needed.
    private var followUp: ((String) -> Void)?
    private var followUpUntil = Date.distantPast
    /// The request a command is answering, for the turn its question shows.
    private var currentRequest: String?
    /// The deterministic command chain, ahead of any model (commands.py).
    private var commands: [VoiceCommand] = []

    init(server: LoopbackServer, stt: Transcriber, shadow: Transcriber?, voice: Voice) {
        self.server = server
        hud = Hud(link: server)
        panels = PanelPublisher(link: server)
        confirm = ConfirmGate(link: server)
        reminders = Reminders(link: server, hud: hud, confirm: confirm)
        cards = Cards(link: server)
        quickNotes = QuickNotes(link: server, hud: hud)
        navigation = Navigation(link: server)
        messages = Messages(hud: hud, confirm: confirm, cards: cards)
        prompts = Prompts(link: server)
        capabilities = CapabilityMenu(link: server, panels: panels,
                                      reminders: reminders,
                                      cards: cards, quickNotes: quickNotes)
        quickNotes.menu = capabilities
        capabilities.navigation = navigation
        capabilities.messages = messages
        messages.onChange = { [capabilities] in capabilities.refreshMessages() }
        messages.onThreadChange = { [capabilities] in capabilities.refreshThread() }
        messages.link = server
        messages.prompts = prompts
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
        // "Open my notes", "show planning": surface opens come first (commands.py).
        commands.append(capabilities)
        // "Reply …", "message Mom saying …" (message_reply.py).
        commands.append(messages)
        messages.say = { [voice] text in voice.say(text) }
        commands.append(SpokenRepliesCommand(voice: voice))
        // "Navigate to the library": the place, then the mode, then the route.
        router.register(navigation)
        commands.append(navigation)
        navigation.receipt = { [hud, voice] text in
            hud.receipt(text)
            voice.say(text)
        }
        // News: the rail tab, every row read in full first (services/news.py).
        panels.register(news)
        news.publish = { [panels] activate in panels.publish("news", activate: activate) }
        news.sendReader = { [panels, server] list in
            var reader = list
            reader["requestId"] = panels.requestId("news")
            server.send(reader)
            Log.add("tx hud.list NEWS · ARTICLE \(panels.requestId("news"))")
        }
        news.say = { [voice] text in voice.say(text) }
        commands.append(news)
        commands.append(PomodoroCommands(pomodoro: pomodoro, panels: panels))
        panels.register(search)
        search.publish = { [panels] activate in panels.publish("search", activate: activate) }
        search.sendReader = { [panels, server] list in
            var reader = list
            reader["requestId"] = panels.requestId("search")
            server.send(reader)
        }
        search.say = { [voice] text in voice.say(text) }
        commands.append(search)
        // Reminders: the glasses' confirm gate, EventKit, the Planner tab and
        // the due page (productivity_actions.py, planner_panel.py, task_monitor.py).
        router.register(confirm)
        router.register(MicResume(link: server))
        router.register(reminders)
        router.register(capabilities)
        router.register(cards)
        panels.register(reminders)
        reminders.publishPanel = { [panels] in panels.publish("planner") }
        reminders.say = { [voice] text in voice.say(text) }
        commands.append(reminders)
        // QuickNotes (voice_note_capture.py, note_commands.py) and its rail tab.
        panels.register(quickNotes)
        quickNotes.publishPanel = { [panels] in panels.publish("quicknotes") }
        quickNotes.say = { [voice] text in voice.say(text) }
        quickNotes.macLink = macLink
        quickNotes.reminders = reminders
        commands.append(quickNotes)
        self.stt = stt
        self.shadow = shadow
        self.voice = voice
        server.onAudio = { [weak stt, weak shadow, quickNotes] samples in
            stt?.feed(samples)
            shadow?.feed(samples)
            DispatchQueue.main.async { quickNotes.audioFrame() }
        }
        // Features keep their state on the main queue.
        stt.onEvent = { [weak self] event in DispatchQueue.main.async { self?.handle(event) } }
        reminders.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        search.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        navigation.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        messages.askFollowUp = { [weak self] question, answer in self?.ask(question, answer: answer) }
        router.register(ConversationInput { [weak self] action in self?.conversationChoice(action) })
        macLink.claude.area = { [navigation] in await navigation.area() }
        navigation.onAsked = { [weak self] question in self?.publish(user: question, assistant: nil, state: "answering") }
        navigation.onAnswer = { [weak self] answer in
            self?.publish(user: nil, assistant: answer, state: "ready")
            self?.voice.say(answer)
        }
    }

    private var insightsTimer: Timer?

    func start() {
        stt.start()
        shadow?.start()
        reminders.start()
        news.start()
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

    private func handle(_ event: Transcriber.Event) {
        let text: String, latency: Int?
        switch event {
        case .volatile(let words):
            if prompts.active { prompts.hearing(words); return }
            quickNotes.hearing(words)
            return
        case .final(let words, let ms):
            text = words
            latency = ms
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
                hud.receipt(reply.text)
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
        // A text just arrived: "reply …" needs no name (message_ui.py
        // handle_unaddressed_reply); the glasses' confirm still gates the send.
        if query == nil, messages.takesUnaddressed(text, now: now) {
            if voice.wasSpeaking(at: spokeUntil) {
                Log.add("own voice ignored: \(text.prefix(80))")
                return
            }
            Log.add("unaddressed reply to the newest text")
            currentRequest = text
            defer { currentRequest = nil }
            if let reply = messages.handle(text), !reply.text.isEmpty {
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
            conversationId = "phone-\(UUID().uuidString.prefix(8).lowercased())"
            turns = []
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
        for command in commands {
            if let reply = command.handle(request) {
                // Empty: the command drives the lens itself (a confirmation,
                // a question) and speaks for itself.
                if reply.text.isEmpty { return }
                Log.add("command answered: \(reply.text.prefix(100))")
                if reply.activated {
                    hud.receipt(reply.text)
                } else {
                    publish(user: request, assistant: reply.text, state: "ready")
                }
                voice.say(reply.text)
                return
            }
        }
        // Everything else goes to the Mac's assistant when it can be reached
        // (the wearer's choice): the conversation so far, the newest last.
        publish(user: request, assistant: nil, state: "answering")
        let conversation = conversationId
        macLink.ask(turns) { [weak self] result in
            guard let self else { return }
            let answer: String
            switch result {
            case .success(let text): answer = text
            case .failure(let failure): answer = MacLink.explain(failure)
            }
            // A newer conversation has started: this answer is stale.
            guard self.conversationId == conversation else { return }
            self.publish(user: nil, assistant: answer, state: "ready")
            self.voice.say(answer)
        }
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

    private func publish(user: String?, assistant: String?, state: String) {
        // JOAT answered (or asked): keep listening without the name.
        if state == "ready", assistant != nil {
            conversationUntil = Date().addingTimeInterval(Self.conversationWindow)
        }
        if let user { turns.append(["role": "user", "text": user]) }
        if let assistant { turns.append(["role": "assistant", "text": assistant]) }
        if turns.count > 16 { turns.removeFirst(turns.count - 16) }
        guard !turns.isEmpty else { return }
        revision += 1
        server.broadcast([
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
