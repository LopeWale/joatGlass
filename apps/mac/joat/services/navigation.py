"""Generation-bound destination repair and provider-neutral navigation session."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import time
from typing import Literal, cast

from ..core.interfaces import GlassesTransport, NavigationProvider
from ..core.navigation import NavigationCandidate, NavigationPoint, ProviderRoute
from ..core.protocol import (
    NavigationAction,
    NavigationHudView,
    NavigationOrigin,
    NavigationOriginFailure,
    NavigationOriginRequest,
    NavigationRerouteRequest,
    NavigationRoute,
    NavigationRouteAck,
    NavigationState,
    NavigationStop,
    NavigationView,
    NavigationViewAck,
    ProtocolError,
)
from .session import Session

log = logging.getLogger(__name__)


# A synchronously rejected phone location request must not erase the locating
# surface before a wearer can perceive it. This affects only immediate failures;
# the request still starts as soon as its HUD state is sent.
_MIN_LOCATING_VISIBLE_SECONDS = 0.8


def navigation_query(text: str) -> str | None:
    """Return an explicit destination request; ordinary speech is untouched."""
    request = text.strip().rstrip(".!?")
    # The normal assistant address is optional, not part of the destination.
    # Keep the command explicit after stripping it: ordinary conversation must
    # never become a route request merely because it mentioned a place.
    request = re.sub(r"^joat(?:\s*[,:-])?\s+", "", request, count=1, flags=re.IGNORECASE)
    match = re.match(
        r"^(?:navigate|directions|take me)(?:\s+to)\s+(.+)$",
        request,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    destination = match.group(1).strip()
    return destination or None


@dataclass(frozen=True, slots=True)
class _Query:
    text: str


@dataclass(frozen=True, slots=True)
class _SearchReady:
    generation: int
    candidates: list[NavigationCandidate] | None


@dataclass(frozen=True, slots=True)
class _RouteReady:
    token: int
    generation: int
    route: ProviderRoute | None
    revision: int
    install_immediately: bool


@dataclass(frozen=True, slots=True)
class _RerouteAfterViewAck:
    origin: NavigationPoint
    generation: int
    revision: int
    request_id: str


class _Cancelled:
    pass


class NavigationSession(Session):
    """One address-confirmation and route lifecycle; one HUD owner."""

    def __init__(
        self,
        query: str,
        provider: NavigationProvider,
        transport: GlassesTransport,
        *,
        session_id: str,
        proximity: NavigationPoint | None = None,
    ) -> None:
        self._initial_query = query
        self._provider = provider
        self._transport = transport
        self._session_id = session_id
        self._proximity = proximity
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._generation = 0
        self._search_generation = 0
        self._search_task: asyncio.Task[None] | None = None
        self._route_task: asyncio.Task[None] | None = None
        self._route_token = 0
        self._current_view: NavigationView | None = None
        self._origin_request: NavigationOriginRequest | None = None
        self._locating_visible_since: float | None = None
        self._candidates: dict[str, NavigationCandidate] = {}
        self._destination: NavigationCandidate | None = None
        self._mode: Literal["walking", "cycling"] | None = None
        self._origin: NavigationPoint | None = None
        self._provider_route: ProviderRoute | None = None
        self._route_revision = 0
        self._route_installed = False
        self._last_state_sequence = 0
        self._install_attempt: tuple[str, int] | None = None
        self._pending_reroute_request_id: str | None = None
        self._reroute_after_view_ack: _RerouteAfterViewAck | None = None
        self._arrived = False
        self._cancelled = False
        self._finished = False
        self._awaiting_destination_speech = False
        self._error_recovery: Literal["destination", "origin", "route", "install"] | None = None

    @property
    def status(self) -> str:
        return "JOAT · navigation"

    @property
    def completion_state(self) -> str:
        return "arrived" if self._arrived else "cancelled"

    async def run(self) -> None:
        self._start_search(self._initial_query)
        try:
            while not self._finished and not self._cancelled:
                event = await self._events.get()
                if isinstance(event, _Cancelled):
                    break
                if isinstance(event, _Query):
                    await self._replace_destination(event.text)
                elif isinstance(event, _SearchReady):
                    await self._handle_search_ready(event)
                elif isinstance(event, NavigationAction):
                    await self._handle_action(event)
                elif isinstance(event, NavigationViewAck):
                    self._handle_view_ack(event)
                elif isinstance(event, NavigationOrigin):
                    await self._handle_origin(event)
                elif isinstance(event, NavigationOriginFailure):
                    await self._handle_origin_failure(event)
                elif isinstance(event, _RouteReady):
                    await self._handle_route_ready(event)
                elif isinstance(event, NavigationRouteAck):
                    await self._handle_route_ack(event)
                elif isinstance(event, NavigationState):
                    self._handle_state(event)
                elif isinstance(event, NavigationRerouteRequest):
                    await self._handle_reroute(event)
        finally:
            tasks = self._cancel_work()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._transport.send(NavigationStop(session_id=self._session_id))

    def cancel(self) -> None:
        self._cancelled = True
        self._awaiting_destination_speech = False
        self._error_recovery = None
        self._locating_visible_since = None
        self._cancel_work()
        self._events.put_nowait(_Cancelled())

    def consume_transcript(self, text: str) -> bool:
        query = text.strip()
        if not self._awaiting_destination_speech or not query:
            return False
        self._awaiting_destination_speech = False
        self._events.put_nowait(_Query(query))
        return True

    async def _replace_destination(self, query: str) -> None:
        # A future repair entry point may be opened while a route is active.
        # Stop the phone-owned tracker before resetting Mac state or sending a
        # new destination to the cloud; old guidance must never outlive the
        # route generation that authorized it.
        if self._route_installed or self._install_attempt is not None:
            await self._transport.send(NavigationStop(session_id=self._session_id))
        self._start_search(query)

    def accept_action(self, action: NavigationAction) -> None:
        self._events.put_nowait(action)

    def accept_origin(self, origin: NavigationOrigin) -> None:
        self._events.put_nowait(origin)

    def accept_origin_failure(self, failure: NavigationOriginFailure) -> None:
        self._events.put_nowait(failure)

    def accept_view_ack(self, ack: NavigationViewAck) -> None:
        self._events.put_nowait(ack)

    def accept_route_ack(self, ack: NavigationRouteAck) -> None:
        self._events.put_nowait(ack)

    def accept_state(self, state: NavigationState) -> None:
        self._events.put_nowait(state)

    def accept_reroute(self, request: NavigationRerouteRequest) -> None:
        self._events.put_nowait(request)

    def _start_search(self, query: str) -> None:
        self._awaiting_destination_speech = False
        self._cancel_task(self._search_task)
        self._cancel_task(self._route_task)
        self._route_token += 1
        self._destination = None
        self._mode = None
        self._origin = None
        self._provider_route = None
        self._origin_request = None
        self._locating_visible_since = None
        self._current_view = None
        self._install_attempt = None
        self._pending_reroute_request_id = None
        self._reroute_after_view_ack = None
        self._route_installed = False
        self._arrived = False
        self._search_generation += 1
        generation = self._search_generation
        log.info("navigation destination search chars=%d generation=%d", len(query), generation)

        async def search() -> None:
            candidates: list[NavigationCandidate] | None
            try:
                candidates = await self._provider.search(query, self._proximity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "navigation destination search failed type=%s",
                    type(exc).__name__,
                )
                candidates = None
            await self._events.put(_SearchReady(generation, candidates))

        self._search_task = asyncio.create_task(search())

    async def _handle_search_ready(self, ready: _SearchReady) -> None:
        if ready.generation != self._search_generation:
            return
        candidates = ready.candidates
        if not candidates:
            self._error_recovery = "destination"
            await self._send_view(NavigationHudView(
                view="error",
                primary="Destination not found",
                secondary="Try the address again",
                hint="tap retry · swipe cancel",
                interaction="stationary",
            ), "search-error")
            return
        bounded = candidates[:2]
        self._candidates = {candidate.choice_id: candidate for candidate in bounded}
        if len(candidates) == 1:
            self._destination = candidates[0]
            await self._show_destination(candidates[0])
            return
        repair_hint = next(
            (candidate.repair_hint for candidate in bounded if candidate.repair_hint),
            None,
        )
        options = [_candidate_option(candidate) for candidate in bounded] + [
            "None of these · retry",
        ]
        choice_ids = [candidate.choice_id for candidate in bounded] + ["retry"]
        await self._send_view(NavigationHudView(
            view="repair",
            primary="Which destination?",
            secondary=(
                f"Check {repair_hint}" if repair_hint else "Choose the canonical place"
            ),
            hint="swipe choose · tap confirm",
            interaction="stationary",
            options=options,
            choice_ids=choice_ids,
            selected_index=0,
        ), "repair")

    async def _show_destination(self, candidate: NavigationCandidate) -> None:
        await self._send_view(NavigationHudView(
            view="destination",
            primary=candidate.label,
            secondary=candidate.address,
            hint="tap confirm · swipe retry",
            interaction="stationary",
        ), "destination")

    async def _show_mode(self) -> None:
        destination = self._destination
        if destination is None:
            return
        await self._send_view(NavigationHudView(
            view="mode",
            primary="Choose travel mode",
            secondary=destination.label,
            hint="swipe choose · tap continue",
            interaction="stationary",
            options=["Walk", "Cycle"],
            choice_ids=["walking", "cycling"],
            selected_index=0,
        ), "mode")

    async def _handle_action(self, action: NavigationAction) -> None:
        view = self._current_view
        if (
            view is None
            or action.session_id != self._session_id
            or action.request_id != view.request_id
            or action.generation != view.generation
        ):
            return
        phase = view.navigation.view
        # A left-temple double press is a universal, explicitly labelled
        # on-glasses stop. It must win during provider/origin waits as well as
        # while guidance is active; those frames intentionally consume normal
        # taps and swipes.
        if action.action == "cancel":
            self.cancel()
            return
        if phase == "destination" and action.action == "confirm_destination":
            await self._show_mode()
        elif phase == "destination" and action.action == "retry_destination":
            await self._show_retry_prompt()
        elif phase == "repair" and action.action == "select_destination":
            if action.choice_id == "retry":
                await self._show_retry_prompt()
                return
            candidate = self._candidates.get(action.choice_id or "")
            if candidate is not None:
                self._destination = candidate
                await self._show_destination(candidate)
        elif phase == "mode" and action.action == "select_mode":
            if action.choice_id not in ("walking", "cycling"):
                return
            self._mode = cast(Literal["walking", "cycling"], action.choice_id)
            await self._request_origin()
        elif phase == "preview" and action.action == "start":
            await self._send_route()
        elif phase == "preview" and action.action == "change_mode":
            await self._show_mode()
        elif phase == "preview" and action.action == "change_destination":
            await self._show_retry_prompt()
        elif action.action == "finish" and self._arrived:
            self._finished = True
            self._events.put_nowait(_Cancelled())
        elif phase == "error" and action.action == "retry":
            if self._error_recovery == "origin":
                await self._request_origin()
            elif self._error_recovery == "route" and self._origin is not None:
                self._start_route(
                    self._origin,
                    self._generation,
                    self._route_revision + 1,
                    install_immediately=False,
                )
            elif self._error_recovery == "install":
                await self._send_route()
            else:
                await self._show_retry_prompt()
    async def _show_retry_prompt(self) -> None:
        self._awaiting_destination_speech = True
        await self._send_view(NavigationHudView(
            view="repair",
            primary="Say the destination again",
            secondary="Keep confirmed parts; correct only what was wrong",
            hint="long-press mic · say address",
            interaction="stationary",
            options=["Waiting for destination"],
            choice_ids=["retry"],
            selected_index=0,
        ), "retry")

    async def _request_origin(self) -> None:
        if self._destination is None or self._mode is None:
            return
        await self._send_view(NavigationHudView(
            view="uncertain",
            primary="Getting your location",
            secondary="Keep phone foreground and unlocked",
            hint="Locating before route search · double-left stops",
            interaction="none",
            mode=self._mode,
        ), "locating")
        self._locating_visible_since = time.monotonic()
        self._generation += 1
        self._origin_request = NavigationOriginRequest(
            session_id=self._session_id,
            request_id=self._request_id("origin", self._generation),
            generation=self._generation,
        )
        self._error_recovery = None
        await self._transport.send(self._origin_request)

    async def _handle_origin(self, origin: NavigationOrigin) -> None:
        request = self._origin_request
        if (
            request is None
            or origin.session_id != request.session_id
            or origin.request_id != request.request_id
            or origin.generation != request.generation
            or self._destination is None
            or self._mode is None
        ):
            return
        self._origin_request = None
        self._locating_visible_since = None
        self._origin = NavigationPoint(origin.latitude, origin.longitude)
        await self._send_view(NavigationHudView(
            view="uncertain",
            primary=f"Finding a {self._mode} route",
            secondary="Checking the route to your destination",
            hint="Route search in progress · double-left stops",
            interaction="none",
            mode=self._mode,
        ), "routing")
        self._start_route(
            self._origin,
            self._generation,
            self._route_revision + 1,
            install_immediately=False,
        )

    async def _handle_origin_failure(self, failure: NavigationOriginFailure) -> None:
        request = self._origin_request
        if (
            request is None
            or failure.session_id != request.session_id
            or failure.request_id != request.request_id
            or failure.generation != request.generation
            or failure.reason != "unavailable"
        ):
            return
        locating_visible_since = self._locating_visible_since
        self._locating_visible_since = None
        if locating_visible_since is not None:
            remaining = _MIN_LOCATING_VISIBLE_SECONDS - (time.monotonic() - locating_visible_since)
            if remaining > 0:
                await asyncio.sleep(remaining)
        if self._cancelled:
            return
        self._origin_request = None
        self._error_recovery = "origin"
        await self._send_view(NavigationHudView(
            view="error",
            primary="Location unavailable",
            secondary="Keep the phone foreground and unlocked",
            hint="tap retry · swipe cancel",
            interaction="stationary",
        ), "origin-error")

    def _handle_view_ack(self, ack: NavigationViewAck) -> None:
        view = self._current_view
        if (
            view is None
            or ack.session_id != view.session_id
            or ack.request_id != view.request_id
            or ack.generation != view.generation
        ):
            return
        if ack.outcome == "rejected":
            self._reroute_after_view_ack = None
            log.warning("navigation HUD view rejected generation=%d", ack.generation)
            self._cancelled = True
            return
        pending = self._reroute_after_view_ack
        if pending is None or pending.request_id != ack.request_id:
            return
        self._reroute_after_view_ack = None
        self._start_route(
            pending.origin,
            pending.generation,
            pending.revision,
            install_immediately=True,
        )

    def _start_route(
        self,
        origin: NavigationPoint,
        generation: int,
        revision: int,
        *,
        install_immediately: bool,
    ) -> None:
        destination = self._destination
        mode = self._mode
        if destination is None or mode is None:
            return
        self._cancel_task(self._route_task)
        self._route_token += 1
        token = self._route_token

        async def route() -> None:
            result: ProviderRoute | None
            try:
                result = await self._provider.route(origin, destination, mode)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "navigation route request failed type=%s",
                    type(exc).__name__,
                )
                result = None
            await self._events.put(_RouteReady(
                token,
                generation,
                result,
                revision,
                install_immediately,
            ))

        self._route_task = asyncio.create_task(route())

    async def _handle_route_ready(self, ready: _RouteReady) -> None:
        if (
            ready.token != self._route_token
            or ready.generation != self._generation
            or self._cancelled
        ):
            return
        if ready.route is None or not self._provider_route_is_usable(ready.route):
            self._pending_reroute_request_id = None
            self._error_recovery = "route"
            await self._send_view(NavigationHudView(
                view="error",
                primary="Route unavailable",
                secondary="Try again or change destination",
                hint="tap retry · swipe cancel",
                interaction="stationary",
            ), "route-error")
            return
        self._provider_route = ready.route
        self._route_revision = ready.revision
        self._generation = max(self._generation, ready.generation)
        if ready.install_immediately:
            await self._send_route()
            return
        mode = self._mode
        if mode not in ("walking", "cycling"):
            return
        remaining = ready.route.plan.steps[-1].distance_along_route_m
        await self._send_view(NavigationHudView(
            view="preview",
            primary=self._destination.label if self._destination else "Destination",
            secondary=f"{mode.title()} · {ready.route.eta_minutes:.0f} min",
            hint="tap start · up mode · down place",
            interaction="stationary",
            mode=mode,
            maneuver="depart",
            remaining_distance_m=remaining,
            eta_minutes=ready.route.eta_minutes,
            progress=0,
        ), "preview")

    def _provider_route_is_usable(self, route: ProviderRoute) -> bool:
        mode = self._mode
        destination = self._destination
        if mode not in ("walking", "cycling") or route.plan.mode != mode or destination is None:
            return False
        try:
            NavigationRoute(
                session_id=self._session_id,
                request_id="validation",
                generation=0,
                route_id=route.route_id,
                revision=1,
                destination_label=destination.label,
                eta_minutes=route.eta_minutes,
                plan=route.plan,
            ).to_wire()
        except ProtocolError:
            return False
        return all(
            earlier.distance_along_route_m <= later.distance_along_route_m
            for earlier, later in zip(route.plan.steps, route.plan.steps[1:])
        )

    async def _send_route(self) -> None:
        route = self._provider_route
        view = self._current_view
        destination = self._destination
        if (
            route is None
            or view is None
            or destination is None
            or self._install_attempt is not None
            or self._route_installed
            or self._arrived
        ):
            return
        # Replace the interactive preview before transmitting the route. This
        # both tells the wearer what is happening and makes a second tap inert
        # while the high-accuracy session is installed on the phone.
        if view.navigation.view == "preview":
            await self._send_view(NavigationHudView(
                view="rerouting",
                primary="Starting navigation",
                secondary="Preparing directions on your glasses",
                hint="Directions begin when ready · double-left stops",
                interaction="none",
                mode=self._mode,
                remaining_distance_m=route.plan.steps[-1].distance_along_route_m,
                eta_minutes=route.eta_minutes,
                progress=0,
            ), "starting")
            view = self._current_view
            if view is None:
                return
        self._route_installed = False
        self._install_attempt = (route.route_id, self._route_revision)
        await self._transport.send(NavigationRoute(
            session_id=self._session_id,
            request_id=view.request_id,
            generation=view.generation,
            route_id=route.route_id,
            revision=self._route_revision,
            destination_label=destination.label,
            eta_minutes=route.eta_minutes,
            plan=route.plan,
        ))

    async def _handle_route_ack(self, ack: NavigationRouteAck) -> None:
        route = self._provider_route
        if (
            route is None
            or self._install_attempt != (ack.route_id, ack.revision)
            or ack.session_id != self._session_id
            or ack.route_id != route.route_id
            or ack.revision != self._route_revision
        ):
            return
        self._install_attempt = None
        if ack.outcome == "installed":
            self._route_installed = True
            self._last_state_sequence = 0
            self._pending_reroute_request_id = None
            return
        self._route_installed = False
        self._pending_reroute_request_id = None
        self._error_recovery = "install"
        await self._send_view(NavigationHudView(
            view="error",
            primary="Navigation could not start",
            secondary="The route was not installed on the phone",
            hint="tap retry · swipe cancel",
            interaction="stationary",
        ), "install-error")

    def _handle_state(self, state: NavigationState) -> None:
        route = self._provider_route
        if (
            self._arrived
            or not self._route_installed
            or route is None
            or state.session_id != self._session_id
            or state.route_id != route.route_id
            or state.revision != self._route_revision
            or state.sequence <= self._last_state_sequence
        ):
            return
        self._last_state_sequence = state.sequence
        if state.state == "arrived":
            self._arrived = True
            self._route_installed = False
            self._install_attempt = None
            self._pending_reroute_request_id = None

    async def _handle_reroute(self, request: NavigationRerouteRequest) -> None:
        route = self._provider_route
        if (
            self._arrived
            or not self._route_installed
            or route is None
            or request.session_id != self._session_id
            or request.route_id != route.route_id
            or request.base_revision != self._route_revision
        ):
            return
        if self._pending_reroute_request_id is not None:
            return
        self._pending_reroute_request_id = request.request_id
        self._route_installed = False
        self._last_state_sequence = 0
        origin = NavigationPoint(request.latitude, request.longitude)
        await self._send_view(NavigationHudView(
            view="rerouting",
            primary="Rerouting",
            secondary="Finding a safer path",
            hint="Old directions paused · double-left stops",
            interaction="none",
            mode=self._mode,
        ), "rerouting")
        view = self._current_view
        if view is None:
            return
        self._reroute_after_view_ack = _RerouteAfterViewAck(
            origin=origin,
            generation=view.generation,
            revision=self._route_revision + 1,
            request_id=view.request_id,
        )

    async def _send_view(self, view: NavigationHudView, phase: str) -> None:
        if view.view != "error":
            self._error_recovery = None
        self._generation += 1
        message = NavigationView(
            session_id=self._session_id,
            request_id=self._request_id(phase, self._generation),
            generation=self._generation,
            navigation=view,
        )
        self._current_view = message
        await self._transport.send(message)

    def _request_id(self, phase: str, generation: int) -> str:
        return f"{self._session_id}:{phase}:{generation}"

    def _cancel_work(self) -> list[asyncio.Task[None]]:
        self._reroute_after_view_ack = None
        tasks = [
            task for task in (self._search_task, self._route_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        self._route_token += 1
        return tasks

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is not None and not task.done():
            task.cancel()


def _candidate_option(candidate: NavigationCandidate) -> str:
    value = f"{candidate.label} · {candidate.address}"
    return value if len(value) <= 64 else f"{value[:61].rstrip()}..."


class NavigationUnavailableSession(Session):
    """Honest wearer-visible result when the Mac has no routing provider."""

    def __init__(
        self,
        transport: GlassesTransport,
        *,
        session_id: str,
        reason: str = "Mapbox is not configured on this Mac",
    ) -> None:
        self._transport = transport
        self._session_id = session_id
        self._settled = asyncio.Event()
        self._request_id = f"{session_id}:provider:1"
        self._reason = reason[:128]

    @property
    def status(self) -> str:
        return "JOAT · navigation unavailable"

    @property
    def completion_state(self) -> str:
        return "unavailable"

    async def run(self) -> None:
        try:
            await self._transport.send(NavigationView(
                session_id=self._session_id,
                request_id=self._request_id,
                generation=1,
                navigation=NavigationHudView(
                    view="error",
                    primary="Navigation unavailable",
                    secondary=self._reason,
                    hint="tap or swipe: close",
                    interaction="stationary",
                ),
            ))
            await self._settled.wait()
        finally:
            await self._transport.send(NavigationStop(session_id=self._session_id))

    def cancel(self) -> None:
        self._settled.set()

    def consume_transcript(self, text: str) -> bool:
        return True

    def accept_action(self, action: NavigationAction) -> None:
        if (
            action.session_id == self._session_id
            and action.request_id == self._request_id
            and action.generation == 1
            and action.action in ("retry", "cancel")
        ):
            self._settled.set()

    def accept_view_ack(self, ack: NavigationViewAck) -> None:
        if (
            ack.session_id == self._session_id
            and ack.request_id == self._request_id
            and ack.generation == 1
            and ack.outcome == "rejected"
        ):
            self._settled.set()
