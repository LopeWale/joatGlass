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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .agents.builtin_tools import messages_tool, notifications_tool
from .agents.grounding import (
    Grounding,
    device_facts,
    local_clock,
    location_facts,
    static_fact,
)
from .agents.location_tools import wearer_location_tools
from .agents.tools import ToolBelt
from .agents.reply_engine import DEFAULT_MODEL, MlxReplyEngine, mlx_available
from .agents.openclaw_gateway import (
    BRAINSTORM_TERMINAL_POLICY,
    CALENDAR_PROPOSAL_TERMINAL_POLICY,
    OpenClawGatewayClient,
    OpenClawGatewayError,
    OpenClawPairingRequired,
)
from .agents.openclaw_native import NativeOpenClawCredentials
from .agents.openclaw_runtime import OpenClawRuntimeConfig, OpenClawTerminalAgent
from .agents.openclaw_terminal import (
    OpenClawToolInventoryUnsettled,
    OpenClawToolsEnabled,
)
from .agents.subprocess_agent import critic, proposer
from .audio.whisper_serve import WhisperServeEngine
from .audio.whisperkit import WhisperKitEngine
from .ble.hub_gateway import HubGatewayTransport
from .core.pairing import PairingStore
from .channels.biome import DEFAULT_STREAM_DIR, BiomeNotificationStore
from .channels.cards import CardLog
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
    BrainstormHudView,
    Hello,
    InputEvent,
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
from .persistence.store import DurableStore
from .persistence.tasks import TaskOccurrenceStore
from .services.hud import HudWriter
from .services.g2_diagnostics import G2DiagnosticStore
from .services.brainstorm_acceptance import (
    try_start_g2_gesture_acceptance,
)
from .services.status import StatusPublisher
from .services.orchestrator import VoiceOrchestrator
from .services.capabilities import CapabilityRouter
from .services.commands import VoiceCommandRouter
from .services.hermes_productivity import (
    HermesCalendarProposalController,
    system_local_time_zone,
)
from .services.planning import personal_data_bridge_state
from .services.message_reply import MessageReplyController
from .services.note_commands import NoteCommandController
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
from .services.models import (
    DEFAULT_CODEX_MODEL, ModelRegistry, default_slots,
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


async def main() -> None:
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
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
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
    parser.add_argument("--location-probe", action="store_true",
                        help="request high-accuracy, privacy-bucketed Gate-0 location telemetry")
    parser.add_argument("-v", "--verbose", action="store_true")
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
    imessage = IMessageStore()

    def wearer_coords() -> tuple[float, float] | None:
        """Return a connected phone's fresh fix without exposing it to the model."""
        loc = transport.fresh_location()
        return (loc.latitude, loc.longitude) if loc is not None else None

    async def refresh_wearer_coords() -> tuple[float, float] | None:
        """Ask the installed Hub plugin for one current iPhone fix."""
        loc = await transport.request_location_refresh()
        return (loc.latitude, loc.longitude) if loc is not None else None

    # Nearby places must be in the reply engine's initial tool schemas.  The
    # provider is also later reused by the separately opt-in navigation flow;
    # configuring it here does not enable route guidance.
    mapbox_token = os.environ.get("MAPBOX_ACCESS_TOKEN", "").strip()
    experimental_navigation = os.environ.get(
        "JOAT_EXPERIMENTAL_NAVIGATION", "",
    ).strip().lower() in {"1", "true", "yes"}
    mapbox_provider: MapboxNavigationProvider | None = None
    if mapbox_token:
        mapbox_provider = MapboxNavigationProvider(
            mapbox_token,
            candidate_navigation_policies(),
        )

    replier: ReplyEngine | None = None
    belt: ToolBelt | None = None
    llm_warm_task: asyncio.Task[bool] | None = None
    if args.no_llm:
        log.info("LLM disabled (--no-llm) — echoing transcripts")
    elif mlx_available():
        # Facts the model cannot know, refreshed per reply. Without this it
        # confabulates them — it answered "2:47 PM" at 4:44 PM.
        grounding = Grounding(
            sources=[local_clock(), device_facts(transport),
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
        ]
        if imessage.available:
            belt_tools.append(messages_tool(imessage))
        belt = ToolBelt(belt_tools)
        mlx_replier = MlxReplyEngine(model_id=args.llm_model, grounding=grounding,
                                     tools=belt,
                                     on_loaded=lambda: status.set(llm="ready"))
        replier = mlx_replier
        llm_warm_task = asyncio.create_task(mlx_replier.warm(), name="mlx-warm")
        log.info("reply engine: %s (+%d tools; warming in background)",
                 args.llm_model, len(belt_tools))
    else:
        log.warning("mlx-lm not installed — echoing transcripts "
                    "(pip install -e '.[mlx]')")

    # "available" is a claim about pip; "ready" is a claim about resident
    # weights. The pill said ready before the model had ever loaded (27).
    status.set(llm="available" if replier is not None else "off")
    hud = HudWriter(transport)
    # Who is at the table is a RUNTIME fact: each collaborator is a binary or
    # a daemon that may simply not be installed. For the Brainstorm table,
    # OpenClaw is the optional third seat, so it is probed rather than assumed
    # — an unreachable gateway costs Brainstorm a voice, never the local
    # productivity boundary.
    # Models the wearer can change by voice while this is running. Slots are
    # read PER TURN, so a switch lands on the next turn instead of the next
    # launch. Whisper is the one that needs work to apply: it holds weights
    # in a resident process, so its slot respawns serve and rolls back if
    # the new model will not load.
    async def _switch_stt(model: str | None) -> bool:
        setter = getattr(stt, "set_model", None)
        if model is None or setter is None:
            return False
        result: bool = await setter(model)
        return result

    models = ModelRegistry(default_slots(
        codex_default=os.environ.get("CODEX_MODEL", DEFAULT_CODEX_MODEL),
        stt_default=args.stt_model,
        stt_apply=_switch_stt,
    ))
    log.info("switchable models — %s", models.summary())

    openclaw_terminal: OpenClawTerminalAgent | None = None
    # Keep the locally constructed, no-tool candidate after an approval or
    # connection failure. A wearer-selected retry may initialize this exact
    # dedicated session; it never falls back to main/Telegram credentials.
    openclaw_retry_candidate: OpenClawTerminalAgent | None = None
    openclaw_calendar_terminal: OpenClawTerminalAgent | None = None
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
                gateway_client = OpenClawGatewayClient(
                    profile=openclaw_config.profile,
                    identity=credentials,
                    token_store=credentials,
                    scopes=requested_scopes,
                    capabilities=("tool-events",),
                    policy=BRAINSTORM_TERMINAL_POLICY,
                )
                candidate = OpenClawTerminalAgent(
                    client=gateway_client,
                    agent_id=openclaw_config.agent_id,
                    policy=BRAINSTORM_TERMINAL_POLICY,
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
                reason = (
                    "native_credentials_unavailable"
                    if isinstance(
                        exc,
                        (NativeBridgeError, NativeBridgeProtocolError, NativeBridgeUnavailable),
                    )
                    else "gateway_not_ready"
                )
                status.set_openclaw(
                    state="failed",
                    endpoint_host=endpoint_host,
                    scopes=(),
                    reason=reason,
                )
                status.set_agent_terminal(
                    state="unavailable",
                    reason="gateway_not_ready",
                )
                log.warning("OpenClaw Agent Terminal unavailable (%s)", type(exc).__name__)
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
    seats: list[tuple[str, str, object]] = [
        ("codex", "proposer", proposer(models.provider("codex"))),
        ("claude", "critic", critic(models.provider("claude"))),
    ]

    def current_seated() -> list[tuple[str, str, object]]:
        terminal = status.agent_terminal_projection()
        remote = openclaw_terminal if terminal["state"] in {"ready", "running"} else None
        current = [(name, role, agent) for name, role, agent in seats if agent]
        if remote is not None:
            current.append(("openclaw", "agent terminal", remote))
        return current

    initial_seated = current_seated()
    absent = ["openclaw"] if openclaw_terminal is None else []
    if len(initial_seated) >= 2:
        log.info("round table: %s%s",
                 ", ".join(f"{n} ({r})" for n, r, _ in initial_seated),
                 f" — absent: {', '.join(absent)}" if absent else "")
    else:
        log.warning("debate unavailable — need 2 collaborators, have %d (absent: %s)",
                    len(initial_seated), ", ".join(absent))

    claim_judge = MiniCheckJudge() if minicheck_available() else None
    if claim_judge:
        log.info("NLI claim judge ready (MiniCheck flan-t5-large; loads on first use)")
    else:
        log.info("NLI claim judge absent — install the 'nli' extra to enable")
    gate = VerifierGate(fetch_page_text, wayback_page_text, claim_judge=claim_judge)

    # One snapshot outlives any single debate: the wearer opens the Debate
    # panel after the fact and must still see what was said.
    debate_state = DebateStateSink()

    navigation_provider: NavigationProvider | None = None
    if mapbox_provider is not None and experimental_navigation:
        navigation_provider = mapbox_provider
        log.info("experimental navigation ready (Mapbox; walking/cycling)")
    elif mapbox_token:
        log.info("experimental navigation disabled (set JOAT_EXPERIMENTAL_NAVIGATION=1)")
    else:
        log.info("navigation provider unavailable (MAPBOX_ACCESS_TOKEN not configured)")

    def make_debate(topic: str) -> Session | None:
        # A debate needs someone to disagree with; one seat is a monologue.
        seated = current_seated()
        if len(seated) < 2:
            return None
        return DebateSession(
            topic,
            seat_participants(seated),
            Broadcast([HudSink(hud, status.agent_terminal_projection), TerminalSink(), debate_state]),
            replier,
            gate=gate,
            on_phase=publish_brainstorm_phase,
        )

    def make_navigation(text: str) -> Session | None:
        destination = navigation_query(text)
        if destination is None:
            return None
        session_id = f"nav-{uuid.uuid4().hex[:20]}"
        provider = navigation_provider
        if provider is None:
            return NavigationUnavailableSession(
                transport,
                session_id=session_id,
                reason=(
                    "Experimental navigation is disabled on this Mac"
                    if mapbox_token
                    else "Mapbox is not configured on this Mac"
                ),
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
        )

    def publish_brainstorm_phase(phase: str) -> None:
        status.set_brainstorm(
            state="running",
            seat_count=len(current_seated()),
            phase=phase,
        )

    def publish_brainstorm_state(state: str) -> None:
        status.set_brainstorm(
            state=state,
            seat_count=len(current_seated()),
            phase="opening" if state == "running" else "closing",
        )

    def topic_capture_view(armed: bool) -> BrainstormHudView:
        """Show the explicit one-utterance capture or its cleared recovery."""
        terminal = status.agent_terminal_projection()
        labels = [
            "Hermes" if name == "openclaw" else name.capitalize()
            for name, _role, _agent in current_seated()
        ]
        return BrainstormHudView(
            screen="brainstorm",
            state="topic_capture" if armed else "ready",
            topic="Speak your question" if armed else "Ready for a question",
            collaborators=labels[:3],
            activity=(
                "Your next spoken phrase starts this Brainstorm."
                if armed else "Tap to choose a question, then speak."
            ),
            terminal_state=str(terminal["state"]),
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        )

    async def show_terminal_retry_view(activity: str) -> None:
        """A retry is visible on the lenses; it is never a silent redial."""
        terminal = status.agent_terminal_projection()
        await hud.show_brainstorm(BrainstormHudView(
            screen="agent_terminal",
            state=str(terminal["state"]),
            topic="Hermes Agent Terminal",
            collaborators=[],
            activity=activity,
            terminal_state=str(terminal["state"]),
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=str(terminal["reason"]),
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
        try:
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
            reason = (
                "native_credentials_unavailable"
                if isinstance(exc, (NativeBridgeError, NativeBridgeProtocolError, NativeBridgeUnavailable))
                else "gateway_not_ready"
            )
            status.set_openclaw(
                state="failed", endpoint_host=endpoint_host, scopes=(), reason=reason,
            )
            status.set_agent_terminal(state="unavailable", reason="gateway_not_ready")
            await show_terminal_retry_view("Hermes is unavailable; retry when the Mac connection recovers.")
        else:
            openclaw_terminal = candidate
            status.set_openclaw(
                state="connected_operator",
                endpoint_host=endpoint_host,
                scopes=requested_scopes,
                reason="",
            )
            status.set_agent_terminal(state="ready", reason="")
            await show_terminal_retry_view("Dedicated no-tool session ready. Tap to begin a Brainstorm.")

    initial_count = len(current_seated())
    status.set(debate=f"{initial_count} seats" if initial_count >= 2 else "unavailable")
    status.set_brainstorm(
        state="ready" if initial_count >= 2 else "unavailable",
        seat_count=initial_count,
        phase="idle",
    )
    sessions = SessionManager(
        hud,
        make_debate,
        card_log=card_log,
        on_state=publish_brainstorm_state,
        starters=[make_navigation],
        terminal_retry=retry_openclaw_terminal,
        topic_capture_view=topic_capture_view,
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
    voice_commands = VoiceCommandRouter(
        message_replies,
        models,
        productivity=productivity_actions,
        notes=note_commands,
        hermes=hermes_productivity,
    )
    # The assistant's name is JOAT — plus mishearings observed LIVE from
    # whisper on short utterances: a one-word utterance gives the model no
    # context, so "JOAT" comes back as "Jokes"/"Jote"/"Jolt". Rejecting
    # those made the assistant look deaf to its own name.
    wake_words = None if args.always_answer else (
        "hey joat",
        "joat", "jote", "jolt", "jokes", "jot",
    )
    orchestrator = VoiceOrchestrator(
        transport, stt, stt_available=stt_available, replier=replier,
        sessions=sessions, wake_words=wake_words, status=status,
        commands=voice_commands,
    )
    orchestrator.attach()

    # The wearer's name from the host account (getUserInfo -> hello), unless
    # the CLI already set one. Grounded once — greet fires on every reconnect.
    named = {"done": bool(args.wearer_name)}

    def greet(hello: Hello) -> None:
        if hello.user_name and not named["done"] and replier is not None:
            grounding.add(static_fact(f"The wearer's name is {hello.user_name}"))
            named["done"] = True
        status.set(glasses="connected", device=f"{hello.model} / {hello.sn}")
        # A topic armed on a replaced Hub connection is no longer an action
        # from the current wearer surface. The new G2 must tap again.
        sessions.clear_topic_capture()
        # Newest-wins socket replacement does not emit the last-disconnect
        # callback. The replacement renderer has no installed route, so the
        # prior Mac session must not continue believing guidance is active.
        active = active_navigation()
        if active is not None:
            active.cancel()
        capabilities.on_disconnect()
        asyncio.get_running_loop().create_task(orchestrator.on_connect())

    transport.on_hello(greet)

    def farewell() -> None:
        # The pills must not lie about a link that is gone.  The serial is
        # part of the live-link projection too: retaining it made a stale
        # disconnected row look like the G2 was still attached.
        status.set(glasses="disconnected", device="", mic="off")
        sessions.clear_topic_capture()
        # A replaced WebContent process has lost its phone-side tracker. Until
        # route rehydration has evidence, keeping the Mac session "installed"
        # would be a stale-direction safety bug.
        active = active_navigation()
        if active is not None:
            active.cancel()
        capabilities.on_disconnect()

    def on_device(device: Device) -> None:
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
        messages=imessage if imessage.available else None, cards=card_log,
        replies=message_replies,
        personal_data=personal_data,
        notes=note_store,
        actions=action_journal,
    )
    transport.on_input(capabilities.on_input)

    async def deliver_task_alert(alert: ProductivityHudView) -> bool:
        return await transport.request_reminder_due_ack(alert)

    task_monitor = TaskMonitor(
        task_occurrences,
        deliver_task_alert,
        evaluate=lambda occurrence: evaluate_reminder_occurrence(occurrence, personal_data),
    )
    task_monitor_task = asyncio.get_running_loop().create_task(task_monitor.run())

    notif_task: asyncio.Task[None] | None = None
    notification_mux: NotificationMux | None = None
    if not args.no_notifications:
        lanes: list[NotificationLane] = []
        if imessage.available:
            lanes.append(NotificationLane(
                "messages-db", imessage, "messages.incoming_text", "authoritative",
                "healthy_complete_for_scope", "complete_for_scope",
                "messages.incoming_text",
            ))
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
        )
        notif_task = asyncio.get_running_loop().create_task(notification_mux.run())

    await transport.start()
    log.info("brain up — plugin dial target is ws://<this-mac>:%d/glasses", args.port)

    # Diagnostic: a starved loop delays every reply, and looks exactly
    # like a slow model in the logs unless it is measured directly.
    lag_task = asyncio.get_running_loop().create_task(orchestrator.watch_loop_lag())
    # Prove liveness between events, or the shell blanks the dashboard.
    beat_task = asyncio.get_running_loop().create_task(status.heartbeat())
    try:
        await shutdown.wait()
    finally:
        remove_shutdown_handlers(shutdown_signals)
        watchdog.cancel()
        lag_task.cancel()
        beat_task.cancel()
        task_monitor.stop()
        task_monitor_task.cancel()
        if llm_warm_task is not None:
            if not llm_warm_task.done():
                llm_warm_task.cancel()
            await asyncio.gather(llm_warm_task, return_exceptions=True)
        await sessions.close()
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
        durable_store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
