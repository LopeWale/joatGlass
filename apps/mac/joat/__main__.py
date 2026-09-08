"""Run the Phase-0 brain:  python -m joat

Stands up the Hub gateway, wires the voice loop, and echoes transcripts to
the HUD. With no whisperkit-cli installed it still runs — the wearer just
sees "brain online (no STT)" instead of transcripts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agents.builtin_tools import conversation_tool, messages_tool, notifications_tool
from .agents.feature_tools import feature_tools, quicknote_reader_tool, terminal_sessions_tool
from .agents.message_tools import message_tools
from .agents.grounding import (
    Grounding,
    device_facts,
    local_clock,
    location_facts,
    static_fact,
)
from .agents.location_tools import wearer_location_tools
from .agents.tools import ToolBelt
from .agents.cli_reply_engine import (
    DEFAULT_WORKDIR, WORKER_PERSONA, ClaudeCliReplyEngine, CodexCliReplyEngine,
    LocalTaskEngine, McpDoor, SeatSwitch, find_codex_binary,
)
from .agents.reply_engine import DEFAULT_MODEL, MlxReplyEngine, mlx_available
from .agents.openclaw_gateway import (
    BRAINSTORM_TERMINAL_POLICY,
    CALENDAR_PROPOSAL_TERMINAL_POLICY,
    OpenClawGatewayClient,
    OpenClawGatewayError,
    OpenClawPairingRequired,
    classify_gateway_dial_failure,
)
from .agents.openclaw_native import NativeOpenClawCredentials
from .agents.openclaw_terminal import PendingApproval
from .agents.openclaw_runtime import (
    make_brainstorm_terminal_agent,
    make_work_terminal_agent,
    OpenClawRuntimeConfig,
    OpenClawTerminalAgent,
    terminal_ready_activity,
)
from .agents.openclaw_terminal import (
    OpenClawToolInventoryUnsettled,
    OpenClawToolsEnabled,
)
from .agents.claude_completer import ClaudeCompleter
from .agents.subprocess_agent import critic, find_claude_binary, proposer
from .audio.router import AudioRouter
from .audio.whisper_serve import WhisperServeEngine
from .audio.whisperkit import WhisperKitEngine
from .ble.hub_gateway import HubGatewayTransport
from .core.pairing import PairingStore
from .channels.biome import DEFAULT_STREAM_DIR, BiomeNotificationStore
from .channels.cards import CardLog
from .channels.contacts import RelationshipBook
from .channels.imessage import IMessageStore
from .channels.message_sender import MessagesAppSender
from .channels.jsonl_source import JsonlNotificationStore
from .channels.notification_mux import NotificationLane, NotificationMux
from .channels.notifications import NotificationStore
from .core.interfaces import NavigationProvider, ReplyEngine, SttEngine
from .core.navigation import NavigationPoint
from .core.protocol import (
    Device,
    BrainstormAction,
    AgentTerminalHudView,
    BrainstormHudView,
    Hello,
    InputEvent,
    Location,
    NavigationAction,
    NavigationOrigin,
    NavigationOriginFailure,
    NavigationRerouteRequest,
    NavigationRouteAck,
    NavigationState,
    NavigationViewAck,
    ProductivityHudView,
)
from .navigation.mapbox import MapboxNavigationProvider
from .navigation.policies import candidate_navigation_policies
from .native.bridge import (
    NativeBridgeClient,
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeUnavailable,
)
from .persistence.actions import ActionJournal
from .persistence.notes import NoteStore
from .persistence.notifications import NotificationObservationStore
from .persistence.pomodoro import PomodoroStore
from .persistence.store import DurableStore
from .persistence.tasks import TaskOccurrenceStore
from .persistence.voice_notes import VoiceNoteStore
from .services.hud import HudWriter
from .services.g2_diagnostics import G2DiagnosticStore
from .services.brainstorm_history import (
    BrainstormHistory,
    BrainstormHistoryCommands,
)
from .services.brainstorm_acceptance import (
    try_start_g2_gesture_acceptance,
)
from .services.status import StatusPublisher
from .services.orchestrator import VoiceOrchestrator
from .services.prompted_speech import PromptedSpeechRuntime
from .services.voice_latency import VoiceLatencyStore
from .services.ambient import AmbientPublisher
from .agents.panel_tools import (
    news_article_tool, news_headlines_tool, pomodoro_status_tool, web_search_tool,
)
from .services.news import NewsService
from .services.panel_commands import NewsCommands, PomodoroCommands, SearchCommands
from .services.panels import PanelPublisher
from .services.pomodoro import PomodoroService
from .services.search import SearchService
from .core.wearer_zone import WearerZone
from .services.capabilities import (
    CapabilityOpenController,
    CapabilityRouter,
    system_home_zone,
)
from .services.terminal_opening import TerminalOpeningController
from .services.agent_terminal import (
    AgentTerminalCommands,
    AgentTerminalRoster,
    LaneAddress,
    TerminalFrame,
)
from .services.commands import VoiceCommandRouter
from .services.hermes_productivity import (
    HermesCalendarProposalController,
    system_local_time_zone,
)
from .services.planning import personal_data_bridge_state
from .services.message_reply import MessageReplyController
from .services.message_ui import MessageUIController
from .services.note_commands import NoteCommandController
from .services.note_insights import (
    NoteInsightCommandController,
    NoteInsightsService,
)
from .services.notes_export import AppleNotesExporter
from .services.voice_note_capture import VoiceNoteCaptureController
from .services.navigation import (
    NavigationSession,
    NavigationUnavailableSession,
    navigation_query,
)
from .services.productivity_actions import ProductivityActionController
from .services.task_monitor import (
    TaskMonitor,
    evaluate_reminder_occurrence,
)
from .services.session import (
    DebateSession, Session, SessionManager, seat_participants,
)
from .services.shutdown import (
    install_shutdown_handlers,
    remove_shutdown_handlers,
    wait_for_supervisor_exit,
)
from .services.agent_endpoint import AgentEndpoint, agent_token
from .services.models import (
    DEFAULT_CODEX_MODEL, ModelRegistry, assistant_slot, default_slots,
)
from .services.sinks import Broadcast, DebateStateSink, HudSink, TerminalSink
from .verifier.fetcher import fetch_page_text, wayback_page_text
from .verifier.nli import MiniCheckJudge, minicheck_available
from .verifier.gate import VerifierGate

log = logging.getLogger("joat")


@dataclass(frozen=True)
class GatewayNetworkPolicy:
    """Listener/authentication boundary selected before the gateway starts.

    A Tailnet Serve reverse proxy reaches its backend from loopback.  That is
    an implementation detail of the proxy, not proof that the caller is the
    wearer's paired Hub package, so the private-home mode must deliberately
    keep the normal pairing check enabled.
    """

    host: str
    trust_loopback: bool


def gateway_network_policy(
    *,
    requested_host: str,
    private_tailnet_gateway: bool,
) -> GatewayNetworkPolicy:
    """Resolve direct-development vs private Tailnet home-brain transport.

    The secure mode has one unambiguous topology: JOAT listens only on IPv4
    loopback and Tailscale Serve supplies the private WSS edge.  Letting an
    explicit non-loopback ``--host`` slip through would silently defeat that
    boundary, so reject it rather than picking an arbitrary precedence rule.
    """

    if not private_tailnet_gateway:
        return GatewayNetworkPolicy(host=requested_host, trust_loopback=True)
    if requested_host not in {"", "0.0.0.0", "::", "127.0.0.1"}:
        raise ValueError(
            "--private-tailnet-gateway cannot be combined with a non-loopback --host"
        )
    return GatewayNetworkPolicy(host="127.0.0.1", trust_loopback=False)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="joat", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--private-tailnet-gateway",
        action="store_true",
        help=(
            "bind the glasses gateway to loopback and require pairing behind "
            "a private Tailscale Serve WSS reverse proxy"
        ),
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stt-binary", default="whisperkit-cli")
    parser.add_argument("--stt-model", default="large-v3_turbo")
    parser.add_argument("--stt-mode", choices=["serve", "batch"], default="serve",
                        help="serve = resident model (~1s/utterance); batch = CLI per utterance (~7s)")
    parser.add_argument("--stt-port", type=int, default=50060)
    parser.add_argument("--stt-prompt",
                        default="JOAT. The wearer addresses the "
                                "assistant as JOAT.",
                        help="vocabulary bias so STT spells the wake word right")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL,
                        help="the resident MLX model: the mind when --agent mlx, "
                             "otherwise the offline fallback and the summariser")
    parser.add_argument("--agent", choices=["claude", "codex", "mlx"], default="claude",
                        help="who fills the mind's seat at boot: claude = Claude Code on "
                             "the Max subscription (default); codex = Codex CLI on the "
                             "ChatGPT subscription; mlx = the resident local model. "
                             "Switchable by voice: 'use codex for the assistant'")
    parser.add_argument("--agent-port", type=int, default=8788,
                        help="loopback port of the OpenAI-shaped endpoint Even AI's "
                             "Agent Configure dials (exposed by Tailscale Serve)")
    parser.add_argument("--no-agent-endpoint", action="store_true",
                        help="do not serve the Even AI agent endpoint")
    parser.add_argument("--wearer-name", default=os.environ.get("JOAT_WEARER", ""),
                        help="the wearer's name, grounded into every reply "
                             "(env JOAT_WEARER)")
    parser.add_argument("--always-answer", action="store_true",
                        help="answer ALL speech (dev only) instead of requiring 'JOAT ...'")
    parser.add_argument("--no-llm", action="store_true", help="echo transcripts only")
    parser.add_argument("--no-notifications", action="store_true",
                        help="disable the Notification Center channel")
    parser.add_argument("--g2-diagnostics", action="store_true",
                        help="opt in to a content-free real-G2 support trace")
    parser.add_argument(
        "--voice-latency-diagnostics",
        action="store_true",
        help="opt in to a bounded, content-free local voice-latency receipt",
    )
    parser.add_argument("--location-probe", action="store_true",
                        help="request high-accuracy, privacy-bucketed Gate-0 location telemetry")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


async def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        gateway_policy = gateway_network_policy(
            requested_host=args.host,
            private_tailnet_gateway=args.private_tailnet_gateway,
        )
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    shutdown = asyncio.Event()
    shutdown_signals = install_shutdown_handlers(shutdown)
    supervisor_pid = os.getppid()

    async def orphan_watchdog() -> None:
        """Request cleanup if the supervising macOS shell disappears."""
        if await wait_for_supervisor_exit(initial_parent_pid=supervisor_pid):
            log.warning("supervisor died — engine shutting down")
            shutdown.set()

    watchdog = asyncio.get_running_loop().create_task(orphan_watchdog())

    status = StatusPublisher()
    durable_store = DurableStore(
        Path.home() / "Library/Application Support/JOAT/joat.sqlite3"
    )
    action_journal = ActionJournal(durable_store)
    recovered = action_journal.recover_after_restart(
        now_us=time.time_ns() // 1000
    )
    if recovered:
        counts: dict[str, int] = {}
        for recovered_state in recovered.values():
            counts[recovered_state] = counts.get(recovered_state, 0) + 1
        log.warning("durable action recovery states=%s", counts)
    g2_diagnostics = (
        G2DiagnosticStore()
        if args.g2_diagnostics or args.location_probe
        else None
    )
    transport = HubGatewayTransport(
        host=gateway_policy.host,
        port=args.port,
        pairing=PairingStore(),
        trust_loopback=gateway_policy.trust_loopback,
        g2_diagnostics=g2_diagnostics,
        location_probe=args.location_probe,
    )
    native_bridge_path = os.environ.get("JOAT_NATIVE_BRIDGE")
    personal_data = (
        NativeBridgeClient(Path(native_bridge_path))
        if native_bridge_path
        else None
    )
    # TCC access in the Mac window is not proof that this app-launched engine
    # can use the owner-only bridge. Publish a bounded, content-free state so
    # the Mac UI and HUD can distinguish those two recovery paths.
    status.set_personal_data(state="checking")

    async def refresh_personal_data_bridge_state() -> None:
        status.set_personal_data(
            state=await personal_data_bridge_state(personal_data),
        )

    bridge_status_task = asyncio.create_task(refresh_personal_data_bridge_state())

    serve_engine: WhisperServeEngine | None = None
    stt: SttEngine
    if args.stt_mode == "serve":
        serve_engine = WhisperServeEngine(
            binary=args.stt_binary, model=args.stt_model, port=args.stt_port,
            prompt=args.stt_prompt,
        )
        stt = serve_engine
    else:
        stt = WhisperKitEngine(binary=args.stt_binary, model=args.stt_model)

    stt_available = getattr(stt, "available", True)
    status.set(stt="ready" if stt_available else "missing")
    if not stt_available:
        log.warning("%s not found — voice loop runs without transcription "
                    "(brew install whisperkit-cli)", args.stt_binary)
    elif serve_engine is not None:
        # Warm the resident model at boot, not on the first utterance.
        log.info("starting whisper serve (model loads now, stays resident)…")
        if not await serve_engine.start(shutdown=shutdown):
            log.warning("serve mode failed — falling back to batch engine")
            stt = WhisperKitEngine(binary=args.stt_binary, model=args.stt_model)
            serve_engine = None
            stt_available = getattr(stt, "available", True)
    status.set(stt="ready" if stt_available else "missing")

    # Cards shown on the HUD are remembered here so "what did I miss?" has a
    # real source instead of a model with no record.
    card_log = CardLog()
    # The overview's tabbed panel: news, pomodoro, search. Each keeps its
    # own truth here and pushes it to the glass proactively; the plugin
    # caches every push so a swipe between tabs never waits on this Mac.
    news = NewsService()
    search = SearchService()
    pomodoro = PomodoroService(
        store=PomodoroStore(durable_store),
        # Loading a checkpoint does not arm it. Activate only after these
        # late-bound owners exist, once the rest of composition is ready.
        on_change=lambda: panels.publish("pomodoro"),
        notify=lambda card: sessions.notify(card),
    )
    panels = PanelPublisher(
        transport, {"news": news, "pomodoro": pomodoro, "search": search},
    )
    imessage = IMessageStore()

    # Where the WEARER is. The phone supplies an IANA name on hello and with
    # every position fix; until one arrives this reads as the Mac's own zone,
    # which is what every time string used unconditionally before.
    wearer_zone = WearerZone(system_home_zone)

    def wearer_coords() -> tuple[float, float] | None:
        """Return a connected phone's fresh fix without exposing it to the model."""
        loc = transport.fresh_location()
        return (loc.latitude, loc.longitude) if loc is not None else None

    async def refresh_wearer_coords() -> tuple[float, float] | None:
        """Ask the installed Hub plugin for one current iPhone fix."""
        loc = await transport.request_location_refresh()
        return (loc.latitude, loc.longitude) if loc is not None else None

    # Nearby places must be in the reply engine's initial tool schemas.  The
    # provider is also later reused by the explicit navigation flow;
    # configuring it here does not start a route by itself.
    mapbox_token = os.environ.get("MAPBOX_ACCESS_TOKEN", "").strip()
    mapbox_provider: MapboxNavigationProvider | None = None
    if mapbox_token:
        mapbox_provider = MapboxNavigationProvider(
            mapbox_token,
            candidate_navigation_policies(),
        )

    # Models the wearer can change by voice while this is running. Slots are
    # read PER TURN, so a switch lands on the next turn instead of the next
    # launch. Whisper is the one that needs work to apply: it holds weights
    # in a resident process, so its slot respawns serve and rolls back if
    # the new model will not load. Built before the mind, which reads its
    # own seat from here on every question.
    async def _switch_stt(model: str | None) -> bool:
        setter = getattr(stt, "set_model", None)
        if model is None or setter is None:
            return False
        result: bool = await setter(model)
        return result

    models = ModelRegistry([
        *default_slots(
            codex_default=os.environ.get("CODEX_MODEL", DEFAULT_CODEX_MODEL),
            stt_default=args.stt_model,
            stt_apply=_switch_stt,
        ),
        assistant_slot("local" if args.agent == "mlx" else args.agent),
    ])
    log.info("switchable models — %s", models.summary())

    replier: ReplyEngine | None = None
    # JOAT's chores (judge, synthesis, rewording) and the Even AI door each
    # answer from their own seats; None means the mind is off entirely.
    worker: ReplyEngine | None = None
    door_replier: ReplyEngine | None = None
    # The same resident MLX weights serve QuickNote summarization through a
    # separate persona-free completion path; None degrades those commands.
    local_completer: MlxReplyEngine | None = None
    belt: ToolBelt | None = None
    llm_warm_task: asyncio.Task[bool] | None = None
    # The shared secret for the Even AI door AND the MCP route the CLI seats
    # use to reach the belt; created once at first boot (0600).
    agent_secret = agent_token()
    if args.no_llm:
        log.info("LLM disabled (--no-llm) — echoing transcripts")
    else:
        # Facts the model cannot know, refreshed per reply. Without this it
        # confabulates them — it answered "2:47 PM" at 4:44 PM.
        grounding = Grounding(
            sources=[local_clock(wearer_zone=wearer_zone), device_facts(transport),
                     location_facts(transport)],
            # Named explicitly, not merely absent: a small model asked to
            # infer "it is not listed, so I should decline" does not — it
            # invents. Weather, messages and location have sources now —
            # the assistant's job is to go get them, not disclaim.
            unknowns=[
                "their calendar or schedule",
            ],
        )
        if args.wearer_name:
            grounding.add(static_fact(f"The wearer's name is {args.wearer_name}"))
        # The belt: what the model can GO GET instead of declining. The
        # wearer's ruling — an assistant that stops at "I don't know" is as
        # fired as one that invents the answer.
        belt_tools = [
            *wearer_location_tools(
                wearer_coords,
                refresh_wearer_coords,
                mapbox_provider.search if mapbox_provider is not None else None,
            ),
            notifications_tool(card_log),
            # The panel tabs, for the questions the exact verbs miss: "is
            # there anything about Apple in the news", "how long until my
            # break", "who won last night". Cache reads and one bounded fetch.
            news_headlines_tool(news),
            # "What does that story say?" — the article behind a headline,
            # read the way the News tab reads it on a tap.
            news_article_tool(news),
            web_search_tool(search, panels),
            pomodoro_status_tool(pomodoro),
        ]
        # Availability can change after startup; the tools report access
        # failures and retry the same read-only store on each request.
        belt_tools.append(messages_tool(imessage))
        # Whole messages, not snippets: the mind reads the same Mac-owned
        # thread the Messages page opens.
        belt_tools.append(conversation_tool(imessage))
        belt = ToolBelt(belt_tools)
        # The mind and the brain are different seats
        # (agents/cli_reply_engine.py): everything above is the brain's —
        # the senses, the hands, the access. The mind is whichever seat the
        # wearer picked: Claude Code on the Max subscription, Codex on the
        # ChatGPT subscription, or the resident MLX model. The CLIs reach
        # the belt over the MCP route of the agent endpoint; never a key.
        mlx_replier: MlxReplyEngine | None = None
        if mlx_available():
            mlx_replier = MlxReplyEngine(model_id=args.llm_model, grounding=grounding,
                                         tools=belt,
                                         on_loaded=lambda: status.set(llm="ready"))
            local_completer = mlx_replier
        door = None if args.no_agent_endpoint else McpDoor(
            url=f"http://127.0.0.1:{args.agent_port}/mcp", token=agent_secret,
        )
        accounts: dict[str, str] = {}

        def note_account(seat: str, state: str) -> None:
            accounts[seat] = state
            status.set(accounts=dict(accounts))

        mind_seats: dict[str, ReplyEngine] = {}
        if mlx_replier is not None:
            mind_seats["local"] = mlx_replier
        mind_seats["claude"] = ClaudeCliReplyEngine(
            find_claude_binary(), grounding=grounding, model=models.provider("claude"), mcp=door,
            on_account=note_account,
        )
        mind_seats["codex"] = CodexCliReplyEngine(
            find_codex_binary(), grounding=grounding, model=models.provider("codex"), mcp=door,
            on_account=note_account,
        )
        seat_switch = SeatSwitch(
            mind_seats, chosen=models.provider("assistant"),
            on_pick=lambda name: status.set(llm="ready", seat=name),
        )
        replier = seat_switch
        # JOAT's own chores — judging a Brainstorm round, the synthesis,
        # rewording a collaborator's answer — get their OWN seats and CLI
        # sessions (the audit's A7): a verdict prompt was landing in the
        # wearer's conversation history, and a chore was waiting on the
        # same lock as their next spoken question. No tools, no facts
        # block; everything a chore needs is in its prompt.
        worker_seats: dict[str, ReplyEngine] = {}
        if mlx_replier is not None:
            worker_seats["local"] = LocalTaskEngine(mlx_replier, WORKER_PERSONA)
        worker_seats["claude"] = ClaudeCliReplyEngine(
            find_claude_binary(), model=models.provider("claude"),
            persona=WORKER_PERSONA, workdir=DEFAULT_WORKDIR / "worker",
            on_account=note_account,
        )
        worker_seats["codex"] = CodexCliReplyEngine(
            find_codex_binary(), model=models.provider("codex"),
            persona=WORKER_PERSONA, workdir=DEFAULT_WORKDIR / "worker",
            on_account=note_account,
        )
        worker = SeatSwitch(worker_seats, chosen=models.provider("assistant"))
        # The Even AI door (A8): the phone sends the whole conversation with
        # every request, so these seats never resume a CLI session of their
        # own — the caller's history is the only history, honoured verbatim.
        door_seats: dict[str, ReplyEngine] = {}
        if mlx_replier is not None:
            door_seats["local"] = mlx_replier
        door_seats["claude"] = ClaudeCliReplyEngine(
            find_claude_binary(), grounding=grounding, model=models.provider("claude"),
            mcp=door, on_account=note_account, session_ttl_s=0.0,
            workdir=DEFAULT_WORKDIR / "door",
        )
        door_seats["codex"] = CodexCliReplyEngine(
            find_codex_binary(), grounding=grounding, model=models.provider("codex"),
            mcp=door, on_account=note_account, session_ttl_s=0.0,
            workdir=DEFAULT_WORKDIR / "door",
        )
        door_replier = SeatSwitch(door_seats, chosen=models.provider("assistant"))
        llm_warm_task = asyncio.create_task(seat_switch.warm(), name="mind-warm")
        log.info("reply engine: subscription seats %s (+%d tools over MCP); boot seat %s",
                 ", ".join(mind_seats), len(belt_tools), args.agent)

    # "available" is a claim about pip; "ready" is a claim about resident
    # weights. The pill said ready before the model had ever loaded (27).
    status.set(llm="available" if replier is not None else "off")
    hud = HudWriter(transport)
    # Who is at the table is a RUNTIME fact: each collaborator is a binary
    # that may simply not be installed. Per docs/BRAINSTORM.md and the
    # wearer's ruling, Brainstorm seats the Codex and Claude CLIs ONLY;
    # Hermes/OpenClaw are Agent Terminal workers and neither participate in
    # a Brainstorm nor gate its availability.

    openclaw_terminal: OpenClawTerminalAgent | None = None
    # Keep the locally constructed, no-tool candidate after an approval or
    # connection failure. A wearer-selected retry may initialize this exact
    # dedicated session; it never falls back to main/Telegram credentials.
    openclaw_retry_candidate: OpenClawTerminalAgent | None = None
    openclaw_calendar_terminal: OpenClawTerminalAgent | None = None
    # The Agent Terminal WORK lane: approvals in, roster reads allowed.
    # Constructed (never initialized) at boot — the adapter connects lazily
    # on the first 'terminal …' send, so a missing operator.approvals grant
    # surfaces as that lane needing recovery, not as a boot failure.
    openclaw_work_terminal: OpenClawTerminalAgent | None = None

    def on_work_approval(approval: PendingApproval) -> None:
        # Late-bound on purpose: approvals can only arrive after a send,
        # and a send requires the roster to exist.
        agent_terminal.note_approval(
            "work", approval.approval_id, approval.kind, approval.summary
        )
    endpoint_host = ""
    requested_scopes: tuple[str, ...] = ()
    try:
        openclaw_config = OpenClawRuntimeConfig.load()
    except ValueError:
        openclaw_config = None
        status.set_openclaw(
            state="failed",
            endpoint_host="",
            scopes=(),
            reason="protocol_rejected",
        )
        status.set_agent_terminal(state="blocked", reason="gateway_not_ready")
        log.warning("OpenClaw Agent Terminal config failed validation")
    if openclaw_config is not None:
        endpoint_host = urlsplit(openclaw_config.profile.url).hostname or ""
        requested_scopes = ("operator.read", "operator.write")
        status.set_openclaw(
            state="checking",
            endpoint_host=endpoint_host,
            scopes=(),
            reason="checking_pairing",
        )
        if personal_data is None:
            status.set_openclaw(
                state="failed",
                endpoint_host=endpoint_host,
                scopes=(),
                reason="native_credentials_unavailable",
            )
            status.set_agent_terminal(
                state="unavailable",
                reason="gateway_not_ready",
            )
        else:
            candidate: OpenClawTerminalAgent | None = None

            def publish_terminal_state(state: str, reason: str) -> None:
                status.set_agent_terminal(state=state, reason=reason)
                if reason in {
                    "connection_closed",
                    "history_recovery_required",
                    "protocol_rejected",
                }:
                    status.set_openclaw(
                        state="disconnected",
                        endpoint_host=endpoint_host,
                        scopes=(),
                        reason=reason,
                    )
                elif state in {"ready", "running"}:
                    status.set_openclaw(
                        state="connected_operator",
                        endpoint_host=endpoint_host,
                        scopes=requested_scopes,
                        reason="",
                    )

            try:
                credentials = await NativeOpenClawCredentials.open(
                    bridge=personal_data,
                    profile=openclaw_config.profile,
                )
                openclaw_work_terminal = make_work_terminal_agent(
                    profile=openclaw_config.profile,
                    credentials=credentials,
                    agent_id=openclaw_config.agent_id,
                    # The agent-terminal status projection belongs to the
                    # Brainstorm-era hermes terminal; the work lane reports
                    # through the roster instead of clobbering it.
                    state_changed=None,
                    on_approval=on_work_approval,
                )
                candidate = make_brainstorm_terminal_agent(
                    profile=openclaw_config.profile,
                    credentials=credentials,
                    agent_id=openclaw_config.agent_id,
                    state_changed=publish_terminal_state,
                )
                openclaw_retry_candidate = candidate
                await candidate.initialize()
            except OpenClawPairingRequired:
                status.set_openclaw(
                    state="approval_required",
                    endpoint_host=endpoint_host,
                    scopes=requested_scopes,
                    reason="device_approval_required",
                )
                status.set_agent_terminal(
                    state="approval_required",
                    reason="write_scope_required",
                )
            except OpenClawToolsEnabled:
                status.set_openclaw(
                    state="disconnected",
                    endpoint_host=endpoint_host,
                    scopes=(),
                    reason="gateway_not_ready",
                )
                status.set_agent_terminal(state="blocked", reason="tools_enabled")
            except OpenClawToolInventoryUnsettled:
                status.set_openclaw(
                    state="disconnected",
                    endpoint_host=endpoint_host,
                    scopes=(),
                    reason="gateway_not_ready",
                )
                status.set_agent_terminal(
                    state="blocked",
                    reason="dedicated_session_not_proven",
                )
            except OpenClawGatewayError as exc:
                status.set_openclaw(
                    state="failed",
                    endpoint_host=endpoint_host,
                    scopes=(),
                    reason="protocol_rejected",
                )
                status.set_agent_terminal(
                    state="unavailable",
                    reason="gateway_not_ready",
                )
                log.warning(
                    "OpenClaw Agent Terminal protocol rejected (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
            except (
                NativeBridgeError,
                NativeBridgeProtocolError,
                NativeBridgeUnavailable,
                RuntimeError,
                OSError,
                TimeoutError,
            ) as exc:
                if isinstance(
                    exc,
                    (NativeBridgeError, NativeBridgeProtocolError, NativeBridgeUnavailable),
                ):
                    reason = "native_credentials_unavailable"
                else:
                    # Keep the exception's own distinction (the audit's
                    # reconnection item): refused means the mini answered
                    # and its Serve front is down; unresolvable means the
                    # tailnet or the host is gone. Both used to collapse
                    # into "gateway_not_ready".
                    reason = classify_gateway_dial_failure(exc).reason_code
                status.set_openclaw(
                    state="failed",
                    endpoint_host=endpoint_host,
                    scopes=(),
                    reason=reason,
                )
                status.set_agent_terminal(
                    state="unavailable",
                    reason=reason,
                )
                log.warning(
                    "OpenClaw Agent Terminal unavailable (%s -> %s)",
                    type(exc).__name__, reason,
                )
            else:
                openclaw_terminal = candidate
                status.set_openclaw(
                    state="connected_operator",
                    endpoint_host=endpoint_host,
                    scopes=requested_scopes,
                    reason="",
                )
                status.set_agent_terminal(state="ready", reason="")
                calendar_client = OpenClawGatewayClient(
                    profile=openclaw_config.profile,
                    identity=credentials,
                    token_store=credentials,
                    scopes=requested_scopes,
                    capabilities=("tool-events",),
                    policy=CALENDAR_PROPOSAL_TERMINAL_POLICY,
                )
                calendar_candidate = OpenClawTerminalAgent(
                    client=calendar_client,
                    agent_id=openclaw_config.agent_id,
                    policy=CALENDAR_PROPOSAL_TERMINAL_POLICY,
                )
                try:
                    await calendar_candidate.initialize()
                except (
                    OpenClawGatewayError,
                    NativeBridgeError,
                    NativeBridgeProtocolError,
                    NativeBridgeUnavailable,
                    RuntimeError,
                    OSError,
                    TimeoutError,
                ) as exc:
                    await calendar_candidate.close()
                    log.warning(
                        "OpenClaw Calendar proposal session unavailable (%s)",
                        type(exc).__name__,
                    )
                else:
                    openclaw_calendar_terminal = calendar_candidate
                    log.info(
                        "OpenClaw Calendar proposals ready in a separate session"
                    )

    # Every OTHER configured Mac contributes its own work lane
    # (AGENT_TERMINAL_ROADMAP.md: "one unified session roster across
    # Hermes, OpenClaw, and configured Tailscale Macs").
    #
    # Constructed, never dialled: make_work_terminal_agent() builds the
    # client without connecting, so a second Mac that is asleep costs an
    # unprepared row the wearer can tap, not a boot failure and not a
    # delay on the Mac that IS awake. That is the same lesson the hermes
    # lane taught — a lane the wearer cannot see is a lane they cannot
    # retry.
    extra_host_lanes: list[tuple[LaneAddress, object]] = []
    if openclaw_config is not None and personal_data is not None:
        try:
            every_host = OpenClawRuntimeConfig.load_all()
        except ValueError:
            every_host = []
        for host_config in every_host:
            if host_config.profile.url == openclaw_config.profile.url:
                continue  # already registered above, without a host label
            try:
                host_credentials = await NativeOpenClawCredentials.open(
                    bridge=personal_data, profile=host_config.profile,
                )
            except (
                OpenClawGatewayError,
                NativeBridgeError,
                NativeBridgeProtocolError,
                NativeBridgeUnavailable,
                RuntimeError,
                OSError,
                TimeoutError,
            ) as exc:
                # One unreachable Mac must not cost the wearer the others.
                log.warning(
                    "OpenClaw host %s unavailable (%s)",
                    host_config.host, type(exc).__name__,
                )
                continue
            extra_host_lanes.append((
                LaneAddress("work", host_config.host),
                make_work_terminal_agent(
                    profile=host_config.profile,
                    credentials=host_credentials,
                    agent_id=host_config.agent_id,
                    state_changed=None,
                    on_approval=on_work_approval,
                ),
            ))
        if extra_host_lanes:
            log.info(
                "Agent Terminal spans %d Macs", len(extra_host_lanes) + 1,
            )
    seats: list[tuple[str, str, object]] = [
        ("codex", "proposer", proposer(models.provider("codex"))),
        ("claude", "critic", critic(models.provider("claude"))),
    ]

    def current_seated() -> list[tuple[str, str, object]]:
        return [(name, role, agent) for name, role, agent in seats if agent]

    def missing_seats() -> list[str]:
        return [name for name, _role, agent in seats if not agent]

    def brainstorm_available() -> bool:
        # A Brainstorm needs someone to disagree with — two CLI seats. It
        # must NOT count any Agent Terminal runtime: an earlier build gated
        # this on the Hermes terminal being ready, which made the wearer's
        # local Codex/Claude conversation hostage to a remote daemon it
        # never speaks to.
        return len(current_seated()) >= 2

    initial_seated = current_seated()
    if brainstorm_available():
        log.info("round table: %s",
                 ", ".join(f"{n} ({r})" for n, r, _ in initial_seated))
    else:
        log.warning("brainstorm unavailable — need the codex and claude CLIs; "
                    "missing: %s", ", ".join(missing_seats()) or "none")

    claim_judge = MiniCheckJudge() if minicheck_available() else None
    if claim_judge:
        log.info("NLI claim judge ready (MiniCheck flan-t5-large; loads on first use)")
    else:
        log.info("NLI claim judge absent — install the 'nli' extra to enable")
    gate = VerifierGate(fetch_page_text, wayback_page_text, claim_judge=claim_judge)

    # One snapshot outlives any single debate: the wearer opens the Debate
    # panel after the fact and must still see what was said.
    # Finished sessions outlive the process: the synthesis the wearer
    # walked away with survives a restart, and Recent has a source.
    brainstorm_history = BrainstormHistory()
    debate_state = DebateStateSink(history=brainstorm_history)

    navigation_provider: NavigationProvider | None = mapbox_provider
    if navigation_provider is not None:
        log.info("navigation ready (Mapbox; walking/cycling)")
    else:
        log.info("navigation provider unavailable (MAPBOX_ACCESS_TOKEN not configured)")

    async def joat_transform(instruction: str, material: str) -> str:
        """JOAT doing what was asked WITH a collaborator's answer.

        The worker seat, not the wearer's conversation: this fires while a
        session is already running two subscriptions, and the rewording
        prompt must not become a turn of the wearer's chat.
        """
        if worker is None:
            return "No model is loaded, so I cannot do that here."
        answer = await worker.reply(
            "You are JOAT. Do exactly what the wearer asks with the text "
            "below, and reply with the RESULT only — no preamble, no "
            "restating the request.\n\n"
            f"Wearer's instruction: {instruction}\n\n"
            f"Text:\n{material}"
        )
        return (answer or "").strip()

    def make_continuation(
        topic: str, prior: tuple[tuple[str, str], ...],
    ) -> Session | None:
        """A continuation is an ordinary session that remembers.

        SESSION_MODEL.md:49 — "continue <title>" resumes the stored
        conversation, so the earlier turns become context the collaborators
        can see. They are NOT counted as positions taken this run; see
        DebateSession._prior.
        """
        session = make_debate(topic)
        if isinstance(session, DebateSession) and prior:
            return DebateSession(
                topic,
                seat_participants(current_seated()),
                Broadcast([HudSink(
                    hud,
                    open_terminal=lambda: capabilities.open_terminal_from_brainstorm(),
                    brainstorm_view_current=lambda: capabilities.brainstorm_view_current,
                ),
                           TerminalSink(), debate_state]),
                worker,
                gate=gate,
                on_phase=publish_brainstorm_phase,
                prior=prior,
            )
        return session

    def make_debate(topic: str) -> Session | None:
        # The SessionManager has already painted the unavailable frame if
        # this is false; retain the guard for direct/internal callers.
        if not brainstorm_available():
            return None
        # A Brainstorm needs someone to disagree with; one seat is a monologue.
        seated = current_seated()
        if len(seated) < 2:
            return None
        return DebateSession(
            topic,
            seat_participants(seated),
            Broadcast([HudSink(
                hud,
                open_terminal=lambda: capabilities.open_terminal_from_brainstorm(),
                brainstorm_view_current=lambda: capabilities.brainstorm_view_current,
            ), TerminalSink(), debate_state]),
            worker,
            gate=gate,
            on_phase=publish_brainstorm_phase,
        )

    def make_navigation(text: str) -> Session | None:
        """The spoken door: "navigate to <place>" and its siblings."""
        destination = navigation_query(text)
        if destination is None:
            return None
        return make_navigation_session(destination)

    def make_navigation_session(destination: str) -> Session | None:
        """One route session for a destination, however it was asked for.

        The mind's start_navigation tool and the spoken grammar both land
        here, so "help me walk to the library" and "navigate to the
        library" open the identical confirm-place / choose-mode flow.
        """
        session_id = f"nav-{uuid.uuid4().hex[:20]}"
        provider = navigation_provider
        if provider is None:
            return NavigationUnavailableSession(
                transport,
                session_id=session_id,
                reason="Mapbox is not configured on this Mac",
            )
        location = transport.location
        proximity = (
            NavigationPoint(location.latitude, location.longitude)
            if location is not None
            else None
        )
        return NavigationSession(
            destination,
            provider,
            transport,
            session_id=session_id,
            proximity=proximity,
            # Same credential as search/routing, and it stays on this Mac:
            # the session sends rendered pixels, never a tile URL.
            map_token=mapbox_token,
        )

    def publish_brainstorm_phase(phase: str) -> None:
        status.set_brainstorm(
            state="running",
            seat_count=len(current_seated()),
            phase=phase,
        )

    def publish_brainstorm_state(state: str) -> None:
        if state == "running":
            capabilities.claim_brainstorm_view()
        status.set_brainstorm(
            state=state,
            seat_count=len(current_seated()),
            phase="opening" if state == "running" else "closing",
        )

    def wearer_terminal_reason(terminal: dict[str, str | bool]) -> str:
        """Translate compact engine state into the exact G2 recovery copy.

        The status projection deliberately stores fixed reason codes. Those
        are useful to the engine but are not an instruction a wearer should
        have to decode on the lenses.
        """

        state = str(terminal["state"])
        reason = str(terminal["reason"])
        if state == "approval_required":
            return "Approve the JOAT device in OpenClaw, then tap retry."
        if state == "unavailable" and reason == "endpoint_missing":
            return "Hermes is not configured on this home brain."
        if state == "unavailable" and reason == "endpoint_refused":
            # The host answered and refused: the mini's Tailscale Serve
            # front is down. The gateway process behind it may be alive
            # (it kept answering Telegram through the 2026-09-06 outage),
            # so the copy must not tell the wearer to restart the wrong
            # thing.
            return "The home brain refused the connection — restore its Tailscale Serve, then tap retry."
        if state == "unavailable" and reason == "host_unreachable":
            return "The home brain is unreachable — check Tailscale on both Macs, then tap retry."
        if state == "unavailable" and reason == "native_credentials_unavailable":
            return "JOAT's gateway credentials are unavailable — restart JOAT on the Mac."
        if state == "unavailable":
            return "Hermes is unavailable; retry when the Mac connection recovers."
        if state == "blocked" and reason == "tools_enabled":
            return "Hermes tools are enabled; this JOAT session remains blocked."
        if state == "blocked" and reason == "dedicated_session_not_proven":
            return "Hermes session isolation is not proven yet; tap retry."
        if state == "running":
            return "Hermes is finishing its current response; wait for ready."
        return "Hermes needs recovery before it can begin a Brainstorm."

    def topic_capture_view(armed: bool) -> BrainstormHudView:
        """Show the explicit one-utterance capture or its cleared recovery."""
        if not brainstorm_available():
            # A missing CLI seat is a Mac-side install problem — name the
            # seat so the wearer knows which binary is gone, and never
            # route them to the Agent Terminal, which has nothing to do
            # with a Brainstorm's availability.
            gone = ", ".join(missing_seats()) or "collaborators"
            return BrainstormHudView(
                state="unavailable",
                topic="Brainstorm",
                collaborators=[n.capitalize() for n, _r, _a in current_seated()],
                activity=f"{gone} CLI missing on the Mac — install to enable.",
                acceptance_armed=False,
            )
        labels = [name.capitalize() for name, _role, _agent in current_seated()]
        return BrainstormHudView(
            state="topic_capture" if armed else "ready",
            topic="Speak your question" if armed else "Ready for a question",
            collaborators=labels[:3],
            activity=(
                "Your next spoken phrase starts this Brainstorm."
                if armed else "Tap to choose a question, then speak."
            ),
            acceptance_armed=args.g2_diagnostics,
        )

    async def show_terminal_retry_view(activity: str) -> None:
        """A retry is visible on the lenses; it is never a silent redial."""
        terminal = status.agent_terminal_projection()
        await hud.show_agent_terminal(AgentTerminalHudView(
            state=str(terminal["state"]),
            focus="hermes",
            lanes=[],
            activity=activity,
            endpoint_host=str(terminal["endpoint_host"]),
            reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        ))

    async def retry_openclaw_terminal() -> None:
        """Retry only the isolated Hermes terminal after an explicit G2 tap."""
        nonlocal openclaw_terminal
        candidate = openclaw_retry_candidate
        if candidate is None:
            await show_terminal_retry_view("Open JOAT on the Mac to configure or approve Hermes.")
            return
        status.set_openclaw(
            state="checking",
            endpoint_host=endpoint_host,
            scopes=(),
            reason="checking_pairing",
        )
        status.set_agent_terminal(state="running", reason="")
        await show_terminal_retry_view("Checking the dedicated Hermes session…")
        was_initialized = candidate.initialized
        try:
            if was_initialized:
                await candidate.attest_existing()
            else:
                await candidate.initialize()
        except OpenClawPairingRequired:
            status.set_openclaw(
                state="approval_required",
                endpoint_host=endpoint_host,
                scopes=requested_scopes,
                reason="device_approval_required",
            )
            status.set_agent_terminal(state="approval_required", reason="write_scope_required")
            await show_terminal_retry_view("Approve the JOAT device in OpenClaw, then tap retry.")
        except OpenClawToolsEnabled:
            status.set_openclaw(
                state="disconnected", endpoint_host=endpoint_host, scopes=(), reason="gateway_not_ready",
            )
            status.set_agent_terminal(state="blocked", reason="tools_enabled")
            await show_terminal_retry_view("Hermes tools are enabled; this JOAT session remains blocked.")
        except OpenClawToolInventoryUnsettled:
            status.set_openclaw(
                state="disconnected", endpoint_host=endpoint_host, scopes=(), reason="gateway_not_ready",
            )
            status.set_agent_terminal(state="blocked", reason="dedicated_session_not_proven")
            await show_terminal_retry_view("Hermes session isolation is not proven yet.")
        except OpenClawGatewayError:
            status.set_openclaw(
                state="failed", endpoint_host=endpoint_host, scopes=(), reason="protocol_rejected",
            )
            status.set_agent_terminal(state="unavailable", reason="gateway_not_ready")
            await show_terminal_retry_view("Hermes rejected the dedicated session protocol.")
        except (
            NativeBridgeError,
            NativeBridgeProtocolError,
            NativeBridgeUnavailable,
            RuntimeError,
            OSError,
            TimeoutError,
        ) as exc:
            if isinstance(exc, (NativeBridgeError, NativeBridgeProtocolError, NativeBridgeUnavailable)):
                reason = "native_credentials_unavailable"
            else:
                reason = classify_gateway_dial_failure(exc).reason_code
            status.set_openclaw(
                state="failed", endpoint_host=endpoint_host, scopes=(), reason=reason,
            )
            status.set_agent_terminal(state="unavailable", reason=reason)
            await show_terminal_retry_view(wearer_terminal_reason({
                "state": "unavailable", "reason": reason, "endpoint_host": endpoint_host,
            }))
        else:
            openclaw_terminal = candidate
            status.set_openclaw(
                state="connected_operator",
                endpoint_host=endpoint_host,
                scopes=requested_scopes,
                reason="",
            )
            status.set_agent_terminal(state="ready", reason="")
            await show_terminal_retry_view(terminal_ready_activity(reverified=was_initialized))

    initial_count = len(current_seated())
    initial_brainstorm_ready = brainstorm_available()
    status.set(debate=f"{initial_count} seats" if initial_brainstorm_ready
               else f"missing: {', '.join(missing_seats()) or 'seats'}")
    status.set_brainstorm(
        state="ready" if initial_brainstorm_ready else "unavailable",
        seat_count=initial_count,
        phase="idle",
    )
    def retry_selected_terminal() -> Awaitable[None]:
        retry = terminal_opening.retry()

        async def run() -> None:
            if not await retry:
                await retry_openclaw_terminal()

        return run()

    def arm_terminal_capture() -> Awaitable[None]:
        """Bind this tap before scheduling its acknowledged question."""
        capture = (
            terminal_commands.begin_capture()
            if not terminal_opening.owns_opening or terminal_opening.current else None
        )

        async def publish() -> None:
            if capture is not None:
                await prompted_speech.publish_terminal_capture(capture)

        return publish()

    async def save_brainstorm_reply(who: str | None) -> str:
        """"Save Claude's reply as a note" — the discussion feeds the notes."""
        found = debate_state.snapshot.last_reply(who)
        if found is None:
            seat = who.capitalize() if who else "any collaborator"
            return f"No reply from {seat} to save yet."
        label, text = found
        receipt = note_commands.save_text(f"{label.split(' (')[0]}: {text}")
        log.info("brainstorm reply saved as note from %s", label)
        return receipt

    sessions = SessionManager(
        hud,
        make_debate,
        card_log=card_log,
        terminal_capture=arm_terminal_capture,
        terminal_view_current=lambda: terminal_opening.current,
        brainstorm_view_current=lambda: capabilities.brainstorm_view_current,
        surface_revision=lambda: capabilities.view_revision,
        on_state=publish_brainstorm_state,
        starters=[make_navigation],
        navigation_factory=make_navigation_session,
        terminal_retry=retry_selected_terminal,
        topic_capture_view=topic_capture_view,
        brainstorm_available=brainstorm_available,
        # "Send Claude's last reply to Hermes", spoken into a live
        # Brainstorm. terminal_commands is composed further down; the
        # lambda defers the lookup to call time, same as capability_opens.
        route_reply=lambda who, lane: terminal_commands.route_reply(who, lane),
        save_reply=save_brainstorm_reply,
        history=brainstorm_history,
        # A tap approves, a swipe denies — see BrainstormController.
        terminal_decide=lambda verdict: agent_terminal.decide_receipt(verdict, None),
        # A tap on a busy terminal with no Brainstorm to return to cycles
        # the focused lane: "switch among sessions" without speaking.
        terminal_cycle=lambda: terminal_opening.cycle(),
        # Resolves "that" once the room has closed. The snapshot outlives
        # the session precisely so a finished answer can still be acted on.
        last_reply=lambda who: debate_state.snapshot.last_reply(who),
        transform=joat_transform,
        continuation_factory=make_continuation,
        # Bound after the message controller/audio router are composed;
        # SessionManager invokes this only for a wearer-requested question.
        publish_topic_capture=lambda capture: prompted_speech.publish_topic_capture(capture),
    )

    def active_navigation() -> NavigationSession | NavigationUnavailableSession | None:
        active = sessions.active_session
        if isinstance(active, (NavigationSession, NavigationUnavailableSession)):
            return active
        return None

    def on_navigation_action(action: NavigationAction) -> None:
        active = active_navigation()
        if active is not None:
            active.accept_action(action)

    def on_navigation_origin(origin: NavigationOrigin) -> None:
        active = active_navigation()
        if isinstance(active, NavigationSession):
            active.accept_origin(origin)

    def on_navigation_origin_failure(failure: NavigationOriginFailure) -> None:
        active = active_navigation()
        if isinstance(active, NavigationSession):
            active.accept_origin_failure(failure)

    def on_navigation_view_ack(ack: NavigationViewAck) -> None:
        active = active_navigation()
        if active is not None:
            active.accept_view_ack(ack)

    def on_navigation_route_ack(ack: NavigationRouteAck) -> None:
        active = active_navigation()
        if isinstance(active, NavigationSession):
            active.accept_route_ack(ack)

    def on_navigation_state(state: NavigationState) -> None:
        active = active_navigation()
        if isinstance(active, NavigationSession):
            active.accept_state(state)

    def on_navigation_reroute(request: NavigationRerouteRequest) -> None:
        active = active_navigation()
        if isinstance(active, NavigationSession):
            active.accept_reroute(request)

    transport.on_navigation_action(on_navigation_action)

    def on_brainstorm_action(action: BrainstormAction) -> None:
        if (action.action == "begin_topic_capture" and not sessions.active) or (
            action.action == "show_brainstorm"
            and isinstance(sessions.active_session, DebateSession)
        ):
            capabilities.claim_brainstorm_view()
        if action.action == "begin_acceptance":
            rejection = try_start_g2_gesture_acceptance(
                transport=transport,
                terminal=lambda: openclaw_terminal,
                trigger=action,
                start_session=sessions.try_start_session,
                acceptance_armed=args.g2_diagnostics,
            )
            if rejection is not None:
                asyncio.get_running_loop().create_task(
                    hud.show_brainstorm(rejection)
                )
            return
        sessions.handle_brainstorm_action(action)

    transport.on_brainstorm_action(on_brainstorm_action)
    transport.on_navigation_origin(on_navigation_origin)
    transport.on_navigation_origin_failure(on_navigation_origin_failure)
    transport.on_navigation_view_ack(on_navigation_view_ack)
    transport.on_navigation_route_ack(on_navigation_route_ack)
    transport.on_navigation_state(on_navigation_state)
    transport.on_navigation_reroute(on_navigation_reroute)
    message_replies = MessageReplyController(
        MessagesAppSender(),
        transport.request_confirmation,
        action_journal,
        begin_reply=lambda: message_ui.begin_reply(),
        on_status=lambda status: message_ui.on_status(status),
        # "my wife" resolves only to a contact the wearer named as such.
        relationships=RelationshipBook(),
        # A draft too long for the confirmation prompt is read in full on
        # the glasses first; capabilities is composed further down.
        review=lambda label, body: capabilities.review_message(label, body),
    )
    task_occurrences = TaskOccurrenceStore(durable_store)

    async def publish_action_status(view: ProductivityHudView) -> None:
        """Render the typed read-only EventKit lifecycle surface on G2."""
        await transport.send(view)

    productivity_actions = ProductivityActionController(
        personal_data,
        transport.request_confirmation,
        action_journal,
        task_occurrences,
        publish_status=publish_action_status,
    )
    hermes_productivity = HermesCalendarProposalController(
        openclaw_calendar_terminal,
        productivity_actions,
        local_time_zone=system_local_time_zone,
    )
    note_store = NoteStore(durable_store)
    note_commands = NoteCommandController(note_store)
    # QuickNote recorder: journal-first meeting/speech capture. The router
    # interposes on the transport's frame stream so an exclusive recording
    # owns the microphone (nothing said mid-meeting reaches the assistant);
    # the orchestrator subscribes through it below.
    voice_note_store = VoiceNoteStore(durable_store)
    recovered_notes = voice_note_store.recover_after_restart(
        now_us=time.time_ns() // 1_000,
    )
    if recovered_notes:
        log.info(
            "recovered %d interrupted QuickNote recordings", len(recovered_notes)
        )
    def capture_started() -> None:
        message_ui.retire()
        prompted_speech.revoke_current()

    audio_router = AudioRouter(transport, on_capture_started=capture_started)
    claude_binary = find_claude_binary()
    note_insights = NoteInsightsService(
        voice_note_store,
        local_completer,
        claude=ClaudeCompleter(claude_binary) if claude_binary else None,
    )
    voice_capture = VoiceNoteCaptureController(
        voice_note_store,
        audio_router,
        stt,
        stt_available=stt_available,
        on_saved=note_insights.schedule_summarize,
    )
    note_insight_commands = NoteInsightCommandController(
        note_insights,
        productivity=productivity_actions,
        exporter=AppleNotesExporter(),
        local_time_zone=system_local_time_zone,
    )
    # Agent Terminal roster: the wearer's existing OpenClaw work sessions,
    # by spoken name. Strictly NOT Brainstorm — these lanes never join the
    # round table, and a missing daemon just means an empty roster.
    # The hermes lane exists whether or not the boot-time dial succeeded
    # (2026-09-06): it used to be registered only from a successful boot
    # connection, so a refused dial at startup left the roster with no
    # hermes row at all, and a gateway that came back later needed an
    # engine restart before the wearer could reach it. The retry candidate
    # is the same lazily-initialisable agent the glasses "retry" tap uses;
    # like the work lane, a row tap prepares it on demand.
    # This Mac's own lanes stay hostless, so a single-host install shows
    # exactly the bare names it always showed. Other Macs carry their host,
    # and the roster prints it only where a name would otherwise be
    # ambiguous — which, for a second "work" lane, is immediately.
    terminal_lanes: list[tuple[str | LaneAddress, object]] = [
        (name, agent) for name, agent in (
            ("work", openclaw_work_terminal),
            ("hermes", openclaw_terminal or openclaw_retry_candidate),
            ("calendar", openclaw_calendar_terminal),
        ) if agent is not None
    ]
    terminal_lanes.extend(extra_host_lanes)

    async def terminal_announce(line: str) -> None:
        # The SessionManager owns the HUD while a session runs; terminal
        # narration must never fight a live Brainstorm for the lenses. The
        # line still lands in the lane's tail either way.
        # An owned Terminal already carries this line in its bound transcript
        # frame. A second unbound text write could repaint after Home.
        if not terminal_opening.owns_opening and capabilities.terminal_view_current:
            await hud.show(line)

    async def terminal_paint(
        frame: TerminalFrame, opening_request_id: str | None = None,
        opening_selection_id: str | None = None,
    ) -> None:
        """Put the roster's own tail on the lenses, densely.

        The agent_terminal screen already reserves a multi-row transcript
        area; before this it only ever carried a status/retry line, so the
        wearer could HEAR their sessions but not SEE them. Same wire, same
        frame kind — the rows were simply never filled.
        """
        # A live Brainstorm owns the lenses — EXCEPT while the wearer has
        # tapped away to the terminal, which is precisely when these frames
        # are the screen they are looking at.
        if not capabilities.terminal_view_current:
            return
        terminal = status.agent_terminal_projection()
        await hud.show_agent_terminal(AgentTerminalHudView(
            state=frame.state,
            focus=frame.topic,
            lanes=frame.lanes,
            activity="\n".join(frame.lines),
            endpoint_host=str(terminal["endpoint_host"]),
            reason=frame.reason,
            no_tools_checked=bool(terminal["no_tools_checked"]),
            approvals=frame.approvals,
            opening_request_id=opening_request_id,
            opening_selection_id=opening_selection_id,
        ))

    agent_terminal = AgentTerminalRoster(
        terminal_lanes,  # type: ignore[arg-type]
        announce=terminal_announce,
        paint=lambda frame: terminal_opening.paint(frame),
    )
    terminal_opening = TerminalOpeningController(
        agent_terminal, paint=terminal_paint,
        connection_generation=lambda: transport.audio_connection_generation,
    )

    terminal_commands = AgentTerminalCommands(
        agent_terminal,
        # The Brainstorm snapshot answers "what did Claude just say?" with
        # the whole reply — live during a session and after it settles.
        last_reply=lambda who: debate_state.snapshot.last_reply(who),
        on_explicit_view=lambda: terminal_opening.claim_legacy(capabilities.claim_terminal_voice),
    )
    # The capability router is composed later; the provider defers the
    # lookup to call time, when it is guaranteed to exist.
    capability_opens = CapabilityOpenController(lambda: capabilities)
    voice_commands = VoiceCommandRouter(
        message_replies,
        models,
        productivity=productivity_actions,
        notes=note_commands,
        voice_notes=voice_capture,
        note_insights=note_insight_commands,
        hermes=hermes_productivity,
        terminal=terminal_commands,
        brainstorm_history=BrainstormHistoryCommands(brainstorm_history),
        capability_opens=capability_opens,
        news=NewsCommands(news, panels),
        pomodoro=PomodoroCommands(pomodoro, panels),
        search=SearchCommands(search, panels),
    )
    # The assistant's name is JOAT — plus mishearings observed LIVE from
    # whisper on short utterances: a one-word utterance gives the model no
    # context, so "JOAT" comes back as "Jokes"/"Jote"/"Jolt". Rejecting
    # those made the assistant look deaf to its own name.
    wake_words = None if args.always_answer else (
        "hey joat",
        "joat", "jote", "jolt", "jokes", "jot",
    )
    voice_latency = VoiceLatencyStore() if args.voice_latency_diagnostics else None
    prompted_speech = PromptedSpeechRuntime(
        hud, message_replies,
        sessions=lambda: sessions,
        terminal=lambda: terminal_commands,
        connection_generation=lambda: transport.audio_connection_generation,
        audio_source=lambda: transport.prompt_audio_source,
        fence_audio=lambda: orchestrator.fence_prompt_audio(),
        capturing=lambda: audio_router.capturing,
        on_message_prompt_retired=lambda action_id: message_ui.cancel(action_id),
    )

    def revoke_message_prompt(action_id: str) -> None:
        ticket = prompted_speech.dispatcher.current_ticket
        if ticket is not None and ticket.owner_kind == "messages" and ticket.owner_id == action_id:
            prompted_speech.dispatcher.revoke(ticket.prompt_id)

    message_ui = MessageUIController(
        message_replies, publish=transport.send,
        after_input=prompted_speech.after_message_input,
        connection_generation=lambda: transport.audio_connection_generation,
        fence_audio=lambda: orchestrator.fence_prompt_audio(),
        capturing=lambda: audio_router.capturing, revoke_prompt=revoke_message_prompt,
    )
    orchestrator = VoiceOrchestrator(
        transport, stt, stt_available=stt_available, replier=replier,
        sessions=sessions, wake_words=wake_words, status=status,
        commands=voice_commands,
        feature_navigation=capability_opens.handle_command,
        latency_observer=voice_latency.record if voice_latency is not None else None,
        frames=audio_router,
        prompted_input=prompted_speech.dispatcher,
        selected_message_reply=message_ui.handle_unaddressed_reply,
        surface_revision=lambda: capabilities.view_revision,
    )
    orchestrator.attach()

    # The wearer's name from the host account (getUserInfo -> hello), unless
    # the CLI already set one. Grounded once — greet fires on every reconnect.
    named = {"done": bool(args.wearer_name)}

    def greet(hello: Hello) -> None:
        if hello.user_name and not named["done"] and replier is not None:
            grounding.add(static_fact(f"The wearer's name is {hello.user_name}"))
            named["done"] = True
        wearer_zone.accept(hello.time_zone)
        status.set(glasses="connected", device=f"{hello.model} / {hello.sn}")
        # A topic armed on a replaced Hub connection is no longer an action
        # from the current wearer surface. The new G2 must tap again.
        prompted_speech.revoke_current()
        sessions.clear_topic_capture()
        # Newest-wins socket replacement does not emit the last-disconnect
        # callback. The replacement renderer has no installed route, so the
        # prior Mac session must not continue believing guidance is active.
        active = active_navigation()
        if active is not None:
            active.cancel()
        capabilities.on_disconnect()
        asyncio.get_running_loop().create_task(orchestrator.on_connect())
        # A plugin that just connected has an empty tab cache. Fill it now
        # so its first swipe onto News paints from memory.
        asyncio.get_running_loop().create_task(panels.publish_all())

    transport.on_hello(greet)

    def farewell() -> None:
        # The pills must not lie about a link that is gone.  The serial is
        # part of the live-link projection too: retaining it made a stale
        # disconnected row look like the G2 was still attached.
        status.set(glasses="disconnected", device="", mic="off")
        prompted_speech.revoke_current()
        sessions.clear_topic_capture()
        # A replaced WebContent process has lost its phone-side tracker. Until
        # route rehydration has evidence, keeping the Mac session "installed"
        # would be a stale-direction safety bug.
        active = active_navigation()
        if active is not None:
            active.cancel()
        capabilities.on_disconnect()

    def on_device(device: Device) -> None:
        prompted_speech.handle_device(device)
        # Battery reaches the dashboard, not just the model.
        hello = transport.hello
        who = f"{hello.model} / {hello.sn}" if hello else "glasses"
        if device.battery is not None:
            charge = "+" if device.charging else ""
            who += f" · {device.battery}%{charge}"
        fields: dict[str, str] = {"device": who}
        if device.mic is not None:
            # TRUTH from the plugin, not the hello-latched guess.
            fields["mic"] = "streaming" if device.mic else "off"
        status.set(**fields)

    transport.on_disconnect(farewell)
    transport.on_device(on_device)

    # One chat.db reader serves BOTH the notification channel and the
    # capability menu's Messages view.
    # Glasses gestures in, drawn views out: the capability menu (Messages /
    # Notifications / Calendar / Tasks / More), the contextual menu's repeat, and
    # the repaint after a menu close blanked the regions.
    capabilities = CapabilityRouter(
        hud=hud, transport=transport, status=status,
        messages=imessage, cards=card_log,
        replies=message_replies,
        message_ui=message_ui,
        personal_data=personal_data,
        notes=note_store,
        voice_notes=voice_note_store,
        voice_capture=voice_capture,
        actions=action_journal,
        acceptance_armed=args.g2_diagnostics,
        # Opening the sidebar row shows the wearer's actual work sessions,
        # not a readiness card. Only configured lanes reach this: with no
        # terminal on the Mac, the router keeps its own fallback copy.
        terminal_view=(
            agent_terminal.repaint if terminal_lanes else None
        ),
        # Opening Brainstorm shows the LAST session, not an invitation the
        # wearer has already accepted. The snapshot outlives the session
        # for exactly this.
        brainstorm_recap=lambda: debate_state.snapshot,
        # SESSION_MODEL.md:46 — opening the row during an active run
        # restores that conversation instead of declaring it finished.
        brainstorm_replay=sessions.replay_active_hud,
        # SESSION_MODEL.md:47 — the idle row is a picker of real sessions.
        # NINE, plus the NEW row, is exactly what the list surface draws:
        # planningFocusRows gives ten (baselines 60..246 on a 20px pitch).
        # Six left four rows of the wearer's own history unused on a screen
        # they have twice told me is too small to waste.
        brainstorm_sessions=lambda: brainstorm_history.sessions(9),
        brainstorm_continue=sessions.resume_by_id,
        # Tap a work session instead of reciting "terminal sessions".
        terminal_rows=agent_terminal.picker_rows,
        terminal_open=terminal_opening.open,
        terminal_current=lambda: agent_terminal.focused,
        terminal_busy=lambda: agent_terminal.busy,
        brainstorm_recent=brainstorm_history.rows,
        # The News / Pomodoro / Search tabs answer their own taps.
        panels=panels,
        wearer_zone=wearer_zone,
    )
    if belt is not None:
        for tool in feature_tools(
            capabilities=capabilities,
            notes=note_commands,
            pomodoro=pomodoro,
            panels=panels,
            brainstorm=sessions,
            terminal=lambda lane, instruction, operation_id: terminal_opening.submit_instruction(
                lane, instruction, operation_id, capabilities.prepare_terminal_instruction,
            ),
            terminal_lanes=agent_terminal.names(),
            # "Help me walk to the library": the same destination flow the
            # spoken "navigate to" grammar opens, reached by conversation.
            navigation=sessions,
        ):
            belt.register(tool)
        for tool in message_tools(message_replies, after_input=prompted_speech.after_message_input):
            belt.register(tool)
        # The mind can read what the wearer keeps and what their terminals
        # are doing, not only write into them.
        belt.register(quicknote_reader_tool(note_store, voice_note_store))
        if agent_terminal.names():
            belt.register(terminal_sessions_tool(agent_terminal))

    def on_capability_input(event: InputEvent) -> None:
        departing = (
            event.container_name == "capability-lifecycle"
            and event.selected_name in {
                "surface-home", "surface-menu", "surface-settings", "surface-news",
                "surface-search", "surface-pomodoro", "surface-feature",
            }
        )
        if departing or (event.container_name == "menu" and event.selected_name == "closed"):
            prompted_speech.revoke_current()
            sessions.clear_topic_capture()
        terminal_was_current = terminal_opening.current
        capabilities.on_input(event)
        if terminal_was_current and not terminal_opening.current:
            prompted_speech.revoke_current()

    transport.on_input(on_capability_input)
    # Paint receipts for the latency record: the plugin's tile-upload trace
    # (diagnostics-only) closes a reply's `first_reply_sent` with `painted`.
    transport.on_trace(orchestrator.on_paint_trace)

    # The resting dashboard's margin facts. Weather is not in the Even Hub
    # SDK, so JOAT resolves it on this Mac from the position the phone
    # already reports, and sends only the finished words.
    # The dashboard's unread number is Messages.app's OWN unread count (the
    # audit's O1: it used to count retained notification cards, battery
    # cards included, and never fell when the wearer read a message on the
    # phone). Counted off the loop thread on a slow cadence and cached: a
    # COUNT over chat.db is not free, and the voice path must never wait on it.
    unread_state = {"count": 0}

    async def refresh_unread_count() -> None:
        while True:
            try:
                unread_state["count"] = await asyncio.to_thread(imessage.unread_count)
            except asyncio.CancelledError:
                raise
            except PermissionError:
                unread_state["count"] = 0  # no access means no claim, not a stale one
            except Exception as exc:
                # A transient read failure keeps the last truthful value.
                log.info("unread count unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(20.0)

    unread_task = asyncio.get_running_loop().create_task(
        refresh_unread_count(), name="unread-count",
    )
    ambient = AmbientPublisher(
        transport,
        unread_count=lambda: unread_state["count"],
    )

    def on_location(fix: Location) -> None:
        ambient.accept_position(fix.latitude, fix.longitude)
        # Landing somewhere new IS a position change, so the correction
        # rides the message that already reports it. Without this the zone
        # would stay wrong until the link happened to drop and re-hello.
        wearer_zone.accept(fix.time_zone)

    transport.on_location(on_location)

    async def deliver_task_alert(alert: ProductivityHudView) -> bool:
        return await transport.request_reminder_due_ack(alert)

    task_monitor = TaskMonitor(
        task_occurrences,
        deliver_task_alert,
        evaluate=lambda occurrence: evaluate_reminder_occurrence(occurrence, personal_data),
        card_log=card_log,
    )
    task_monitor_task = asyncio.get_running_loop().create_task(task_monitor.run())
    # The feeds are polled on the wearer's cadence (news.json, default every
    # fifteen minutes) and pushed after every pass; the first pass is what
    # fills the News tab before anyone asks.
    news_task = asyncio.get_running_loop().create_task(
        news.run(on_refreshed=lambda: panels.publish("news")),
    )

    # NOT started: macOS already owns calendar alerting. An event carries its
    # own EKAlarm(s) (EKCalendarItem.alarms, with relativeOffset/absoluteDate),
    # the OS fires them at the time the wearer configured, and those alerts
    # reach the glasses through the Notification Center banner lane below as
    # `calendar.banner`. A second poller here announced on a hardcoded 10-minute
    # lead, so it both ignored the wearer's chosen alarm and double-announced
    # every event that had one. The platform is the right owner; the module and
    # its store remain only until they can be deleted.

    notif_task: asyncio.Task[None] | None = None
    notification_mux: NotificationMux | None = None
    if not args.no_notifications:
        # The mux already retries baseline failures independently. Retain
        # this lane when Messages is not yet accessible at startup.
        lanes: list[NotificationLane] = [NotificationLane(
            "messages-db", imessage, "messages.incoming_text", "authoritative",
            "healthy_complete_for_scope", "complete_for_scope",
            "messages.incoming_text",
        )]
        # Keep the watcher lane even before its first file exists. It retries
        # independently, so Messages never masks Mail/Calendar/Reminder/general banners.
        lanes.append(NotificationLane(
            "ax-banners", JsonlNotificationStore(), "notifications.banner", "best_effort",
            "healthy_best_effort", "best_effort",
        ))
        if DEFAULT_STREAM_DIR.is_dir():
            lanes.append(NotificationLane(
                "biome-delivery", BiomeNotificationStore(), "notifications.delivery_metadata",
                "metadata_only", "healthy_best_effort", "metadata_only",
            ))
        elif NotificationStore().available:
            lanes.append(NotificationLane(
                "usernoted-db", NotificationStore(), "notifications.banner", "best_effort",
                "healthy_best_effort", "best_effort",
            ))
        def full_message_text(channel: str, preview: str, at: float) -> str | None:
            """Recover a macOS-elided Messages banner from chat.db.

            Only Messages: it is the one channel whose authoritative store
            this Mac can read. Everything else keeps the rendered preview.
            """
            if channel != "Messages" or not imessage.available:
                return None
            return imessage.full_text_near(preview, at)

        notification_mux = NotificationMux(
            lanes,
            NotificationObservationStore(durable_store),
            # Delivery must not form a 5-second-per-item head-of-line queue.
            # Preserve raw content and use deterministic policy until triage
            # has its own bounded, fair worker pool.
            triage=None,
            deliver=sessions.notify_and_wait,
            status=status,
            poll_interval_s=1.0,
            enrich_body=full_message_text,
        )
        notif_task = asyncio.get_running_loop().create_task(notification_mux.run())

    pomodoro.activate_restored()
    await transport.start()
    log.info("brain up — plugin dial target is ws://<this-mac>:%d/glasses", args.port)

    # The door Even AI knocks on (services/agent_endpoint.py): Even's own
    # assistant, pointed at this brain through Agent Configure. Loopback
    # only; Tailscale Serve carries it to the phone over HTTPS.
    agent_endpoint: AgentEndpoint | None = None
    if replier is not None and not args.no_agent_endpoint:
        agent_endpoint = AgentEndpoint(
            # The door's own seats: no resumed CLI session, so the history
            # the Even app sends with each request is the whole memory.
            door_replier or replier, agent_secret, port=args.agent_port, tools=belt,
            model_label=lambda: models.current("assistant") or "joat",
        )
        try:
            await agent_endpoint.start()
        except OSError as exc:
            agent_endpoint = None
            log.warning("agent endpoint not started (%s) — Even AI cannot reach "
                        "the brain until it is", exc)
        else:
            log.info("Even AI agent endpoint: http://127.0.0.1:%d/v1/chat/completions "
                     "(expose with: tailscale serve --bg --set-path /agent "
                     "http://127.0.0.1:%d)", agent_endpoint.port, agent_endpoint.port)

    # Diagnostic: a starved loop delays every reply, and looks exactly
    # like a slow model in the logs unless it is measured directly.
    lag_task = asyncio.get_running_loop().create_task(orchestrator.watch_loop_lag())
    # Prove liveness between events, or the shell blanks the dashboard.
    beat_task = asyncio.get_running_loop().create_task(status.heartbeat())
    try:
        await shutdown.wait()
    finally:
        remove_shutdown_handlers(shutdown_signals)
        # Stop new questions before draining their owners. This does not
        # cancel completed STT or an unrelated caller's tool execution.
        prompted_speech.begin_shutdown()
        message_ui.retire()
        # Close the capture lane first: an in-flight recording is journaled
        # as interrupted (raw retained) rather than silently truncated.
        voice_capture.interrupt_for_shutdown()
        watchdog.cancel()
        lag_task.cancel()
        beat_task.cancel()
        unread_task.cancel()
        task_monitor.stop()
        task_monitor_task.cancel()
        news.stop()
        news_task.cancel()
        # Stop timer callbacks and drain publications before their owners
        # close. Preserve the saved deadline for the next engine start.
        await pomodoro.aclose()
        if llm_warm_task is not None:
            if not llm_warm_task.done():
                llm_warm_task.cancel()
            await asyncio.gather(llm_warm_task, return_exceptions=True)
        await sessions.close()
        if agent_endpoint is not None:
            await agent_endpoint.stop()
        await message_replies.aclose()
        await message_ui.aclose()
        await prompted_speech.aclose()
        await transport.stop()
        if notif_task is not None:
            if notification_mux is not None:
                notification_mux.stop()
            notif_task.cancel()
        if serve_engine is not None:
            await serve_engine.close()
        if openclaw_calendar_terminal is not None:
            await openclaw_calendar_terminal.close()
        if openclaw_terminal is not None:
            await openclaw_terminal.close()
        if not bridge_status_task.done():
            bridge_status_task.cancel()
        await asyncio.gather(bridge_status_task, return_exceptions=True)
        if personal_data is not None:
            await personal_data.close()
        if voice_latency is not None:
            await voice_latency.flush()
        durable_store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
