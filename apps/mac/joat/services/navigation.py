"""Generation-bound destination repair and provider-neutral navigation session."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import math
import re
import time
from typing import Literal, cast

from ..core.interfaces import GlassesTransport, NavigationProvider
from ..navigation.map_image import MapImageError, map_move_meters, render_map_tile
from ..core.navigation import NavigationCandidate, NavigationPoint, ProviderRoute
from ..core.protocol import (
    NavigationMapPanel,
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


# Voice is deliberately a small, exact grammar while navigation owns the
# session.  It prevents a conversational fragment from unexpectedly changing
# a route, while letting the wearer complete every visible navigation choice
# hands-free.
_VOICE_CONFIRM = frozenset({"confirm", "yes", "correct", "continue", "that one"})
_VOICE_RETRY = frozenset({"retry", "try again", "retry navigation"})
_VOICE_REPAIR = frozenset({
    "retry", "try again", "change destination", "change the destination",
    "different destination", "not this one", "no",
})
_VOICE_FIRST = frozenset({"first", "the first", "option one", "option 1"})
_VOICE_SECOND = frozenset({"second", "the second", "option two", "option 2"})
_VOICE_WALK = frozenset({"walk", "walking", "walking mode"})
_VOICE_CYCLE = frozenset({"cycle", "cycling", "bike", "bicycle", "cycling mode"})
_VOICE_START = frozenset({"start", "start navigation", "go", "begin", "begin navigation"})
_VOICE_CHANGE_MODE = frozenset({"change mode", "change the mode"})
_VOICE_FINISH = frozenset({"finish", "done", "end navigation"})
_VOICE_CANCEL = frozenset({
    "stop", "cancel", "stop navigation", "cancel navigation", "end directions",
    "stop directions",
})

_NEARBY_PREFIX = re.compile(
    r"^(?:the\s+)?(?:nearest|closest)\s+",
    flags=re.IGNORECASE,
)
_NEARBY_SUFFIX = re.compile(
    r"\s*,?\s*(?:near(?:\s+to)?\s+(?:me|here|us)|around\s+(?:me|here|us)|"
    r"close\s+by|nearby|to\s+(?:me|here|us))$",
    flags=re.IGNORECASE,
)
_CORRECTION_PREFIX = re.compile(
    r"^(?:no\s*[,;:-]?\s*)?i\s+(?:mean|meant)\s+(.+)$",
    flags=re.IGNORECASE,
)
_EARTH_RADIUS_METERS = 6_371_008.8

ProximityRefresh = Callable[[], Awaitable[NavigationPoint | None]]


def _normalise_navigation_voice(text: str) -> str:
    """Canonicalise only presentation noise; do not reinterpret speech."""
    return re.sub(r"\s+", " ", text.casefold().strip().rstrip(".!?"))


def navigation_query(text: str) -> str | None:
    """Return an explicit destination request; ordinary speech is untouched."""
    request = text.strip().rstrip(".!?")
    # The normal assistant address is optional, not part of the destination.
    # Keep the command explicit after stripping it: ordinary conversation must
    # never become a route request merely because it mentioned a place.
    request = re.sub(r"^joat(?:\s*[,:-])?\s+", "", request, count=1, flags=re.IGNORECASE)
    request = re.sub(
        r"^(?:(?:can|could|would)\s+you\s+|please\s+)",
        "",
        request,
        count=1,
        flags=re.IGNORECASE,
    )
    match = re.match(
        r"^(?:navigate(?:\s+me)?|directions|take me|guide me|get me|"
        r"help me get|show me (?:the )?way)(?:\s+to)\s+(.+)$",
        request,
        flags=re.IGNORECASE,
    )
    if match is None:
        match = re.match(
            r"^(?:how do i get|what(?:'s| is) the (?:best )?way)(?:\s+to)\s+(.+)$",
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
class _DestinationSearch:
    text: str
    nearby: bool


@dataclass(frozen=True, slots=True)
class _SearchReady:
    generation: int
    candidates: list[NavigationCandidate] | None
    proximity: NavigationPoint | None
    nearby: bool
    failure: Literal["location"] | None = None


#: How far the wearer must move before the street panel is refetched.
#:
#: This is a distance on the PANEL, not an arbitrary round number. At zoom
#: 16 and this latitude the tile runs about 1.95 m per pixel, so the old
#: 120 m let the wearer drift ~62 px from the centre of a 208 px-tall pane —
#: 60% of the way to the edge, with the ground ahead of them scrolled off
#: and their own marker sitting in the bottom corner. 60 m halves that to
#: under a third of the half-pane, which keeps the road ahead in view.
#:
#: The cost is bounded: each refresh is one Mapbox fetch and one image send
#: (~0.5-2 s over BLE), and at walking pace 60 m is about 45 seconds apart.
_MAP_REFRESH_METERS = 60.0

_ROUTE_FAILURE_COPY: dict[str, tuple[str, str]] = {
    # Each one names a different next move for the wearer.
    "no_route": ("No route from here", "Try cycling or a closer destination"),
    "too_long": ("Route too long for the HUD", "Choose a closer destination"),
    "unreachable": ("Can't reach the map service", "Check this Mac's internet, then retry"),
    "incomplete": ("Route data was incomplete", "Try again or change destination"),
    "rejected": ("Route unavailable", "Try again or change destination"),
}


@dataclass(frozen=True, slots=True)
class _RouteReady:
    token: int
    generation: int
    route: ProviderRoute | None
    revision: int
    install_immediately: bool
    #: Why the route failed, when it did. One blind "Route unavailable" for a
    #: dead network, an unreachable destination, and an over-long route left
    #: the wearer with no next move.
    failure: str | None = None


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
        refresh_proximity: ProximityRefresh | None = None,
        map_token: str = "",
    ) -> None:
        self._initial_query = query
        self._provider = provider
        self._transport = transport
        self._session_id = session_id
        self._proximity = proximity
        self._refresh_proximity = refresh_proximity
        self._search_proximity: NavigationPoint | None = None
        self._nearby_search = False
        self._last_query = query
        # Street surroundings for the guidance pane. Empty means the wearer
        # still gets the route's shape; the map is an enhancement, never a
        # precondition for navigating.
        self._map_token = map_token
        self._map_task: asyncio.Task[None] | None = None
        self._map_centre: NavigationPoint | None = None
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
        self._error_recovery: Literal[
            "destination", "search_location", "origin", "route", "install"
        ] | None = None

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
        """Consume an explicit, state-bound navigation voice command.

        Navigation must remain usable when a wearer cannot safely perform a
        temple gesture.  Speech is therefore an equal input path, but never a
        shortcut around the canonical-place confirmation: each utterance maps
        only to the action valid for the currently visible navigation view.
        """
        query = text.strip()
        if not query:
            return False

        # An explicit new destination supersedes the current in-flight search
        # or route.  Ordinary place mentions deliberately do not do this.
        replacement = navigation_query(query)
        if replacement is not None:
            self._awaiting_destination_speech = False
            self._events.put_nowait(_Query(replacement))
            return True

        spoken = _normalise_navigation_voice(query)
        if spoken in _VOICE_CANCEL:
            self.cancel()
            return True

        view = self._current_view
        if view is not None and view.navigation.view in ("destination", "repair", "error"):
            correction = _navigation_correction(query)
            if correction is not None:
                self._awaiting_destination_speech = False
                self._events.put_nowait(_Query(correction))
                return True

        if self._awaiting_destination_speech:
            self._awaiting_destination_speech = False
            self._events.put_nowait(_Query(query))
            return True

        if view is None:
            return False
        decision = self._voice_action(view, spoken)
        if decision is None:
            return False
        action, choice_id = decision
        self._events.put_nowait(NavigationAction(
            session_id=view.session_id,
            request_id=view.request_id,
            generation=view.generation,
            action=action,
            choice_id=choice_id,
        ))
        return True

    def _voice_action(
        self,
        view: NavigationView,
        spoken: str,
    ) -> tuple[str, str | None] | None:
        """Return only an action the current visible state permits."""
        phase = view.navigation.view
        if phase == "destination":
            if spoken in _VOICE_CONFIRM:
                return ("confirm_destination", None)
            if spoken in _VOICE_REPAIR:
                return ("retry_destination", None)
            return None

        if phase == "repair":
            choices = view.navigation.choice_ids or []
            if spoken in _VOICE_REPAIR:
                return ("select_destination", "retry")
            if spoken in _VOICE_FIRST and len(choices) >= 1:
                return ("select_destination", choices[0])
            if spoken in _VOICE_SECOND and len(choices) >= 2:
                return ("select_destination", choices[1])
            for choice_id in choices:
                candidate = self._candidates.get(choice_id)
                if candidate is not None and spoken in {
                    _normalise_navigation_voice(candidate.label),
                    _normalise_navigation_voice(candidate.address),
                }:
                    return ("select_destination", choice_id)
            return None

        if phase == "mode":
            if spoken in _VOICE_WALK:
                return ("select_mode", "walking")
            if spoken in _VOICE_CYCLE:
                return ("select_mode", "cycling")
            return None

        if phase == "preview":
            if spoken in _VOICE_START:
                return ("start", None)
            if spoken in _VOICE_CHANGE_MODE:
                return ("change_mode", None)
            if spoken in _VOICE_REPAIR:
                return ("change_destination", None)
            return None

        if phase == "error" and spoken in _VOICE_RETRY:
            return ("retry", None)
        if self._arrived and spoken in _VOICE_FINISH:
            return ("finish", None)
        return None

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
        search_request = _destination_search(query)
        self._last_query = query
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
            proximity = self._proximity
            if search_request.nearby and self._refresh_proximity is not None:
                try:
                    refreshed = await self._refresh_proximity()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "navigation proximity refresh failed type=%s",
                        type(exc).__name__,
                    )
                    refreshed = None
                if refreshed is not None:
                    proximity = refreshed
            if search_request.nearby and proximity is None:
                await self._events.put(_SearchReady(
                    generation,
                    None,
                    None,
                    True,
                    "location",
                ))
                return
            try:
                candidates = await self._provider.search(search_request.text, proximity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "navigation destination search failed type=%s",
                    type(exc).__name__,
                )
                candidates = None
            await self._events.put(_SearchReady(
                generation,
                candidates,
                proximity,
                search_request.nearby,
            ))

        self._search_task = asyncio.create_task(search())

    async def _handle_search_ready(self, ready: _SearchReady) -> None:
        if ready.generation != self._search_generation:
            return
        if ready.proximity is not None:
            # Keep the accepted request-bound fix for a conversational
            # correction in this same session. A follow-up such as "I meant
            # OnCue, the gas station" should not fall back to an older fix.
            self._proximity = ready.proximity
        self._search_proximity = ready.proximity
        self._nearby_search = ready.nearby
        if ready.failure == "location":
            self._error_recovery = "search_location"
            await self._send_view(NavigationHudView(
                view="error",
                primary="Phone location unavailable",
                secondary="I need your phone location to find the nearest match",
                hint="tap/say retry · swipe cancel",
                interaction="stationary",
            ), "search-location-error")
            return
        candidates = ready.candidates
        if not candidates:
            self._error_recovery = "destination"
            await self._send_view(NavigationHudView(
                view="error",
                primary="Destination not found",
                secondary="Try the address again",
                hint="tap/say retry · swipe cancel",
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
        options = [
            _candidate_option(
                candidate,
                proximity=ready.proximity if ready.nearby else None,
            )
            for candidate in bounded
        ] + [
            "None of these · retry",
        ]
        choice_ids = [candidate.choice_id for candidate in bounded] + ["retry"]
        await self._send_view(NavigationHudView(
            view="repair",
            primary="Which destination?",
            secondary=(
                "Closest matches near you"
                if ready.nearby
                else (
                    f"Check {repair_hint}" if repair_hint else "Choose the canonical place"
                )
            ),
            hint="tap choose · swipe select · say first/second",
            interaction="stationary",
            options=options,
            choice_ids=choice_ids,
            selected_index=0,
        ), "repair")

    async def _show_destination(self, candidate: NavigationCandidate) -> None:
        secondary = candidate.address
        if self._nearby_search and self._search_proximity is not None:
            distance = _candidate_distance(candidate, self._search_proximity)
            secondary = f"About {_format_distance(distance)} away · {candidate.address}"
        await self._send_view(NavigationHudView(
            view="destination",
            primary=candidate.label,
            secondary=secondary[:128],
            hint="tap/say confirm · swipe retry",
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
            hint="tap choose · swipe select · say walk/cycle",
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
            if self._error_recovery == "search_location":
                self._start_search(self._last_query)
            elif self._error_recovery == "origin":
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
            hint="say the destination again",
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
            hint="Finding phone location · say stop",
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

    def _refresh_map_panel(self, origin: NavigationPoint) -> None:
        """Fetch the streets around the wearer, at most one request in flight.

        Throttled by distance, not by time: standing still costs nothing,
        and a HUD panel is only stale once the wearer has actually walked
        out of it.
        """
        if not self._map_token:
            return
        centre = self._map_centre
        if centre is not None and map_move_meters(
            centre.latitude, centre.longitude, origin.latitude, origin.longitude,
        ) < _MAP_REFRESH_METERS:
            return
        if self._map_task is not None and not self._map_task.done():
            return
        self._map_centre = origin
        session_id = self._session_id

        async def fetch() -> None:
            try:
                tile = await render_map_tile(
                    self._map_token, origin.latitude, origin.longitude,
                )
            except (MapImageError, OSError) as exc:
                log.info("navigation map unavailable: %s", type(exc).__name__)
                return
            except asyncio.CancelledError:
                raise
            if self._cancelled or self._session_id != session_id:
                return
            await self._transport.send(NavigationMapPanel(
                session_id=session_id,
                latitude=tile.latitude,
                longitude=tile.longitude,
                zoom=tile.zoom,
                width=tile.width,
                height=tile.height,
                png_base64=base64.b64encode(tile.png).decode("ascii"),
            ))

        self._map_task = asyncio.get_running_loop().create_task(fetch())

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
        self._refresh_map_panel(self._origin)
        await self._send_view(NavigationHudView(
            view="uncertain",
            primary=f"Finding a {self._mode} route",
            secondary="Checking the route to your destination",
            hint="Route search in progress · say stop",
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
            primary="Phone location unavailable",
            secondary="Allow Location for Even Hub, then retry",
            hint="tap/say retry · swipe cancel",
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
                # A provider that declares `reason` is promising a bounded,
                # token-free failure (NavigationProvider contract), so its
                # message is safe to log. Anything else is logged by type
                # only: a raw urllib error can carry the access token in its
                # URL, and that must never reach a log file.
                failure = getattr(exc, "reason", None)
                log.warning(
                    "navigation route request failed type=%s reason=%s detail=%s",
                    type(exc).__name__,
                    failure or "unknown",
                    str(exc)[:200] if isinstance(failure, str) else "",
                )
                await self._events.put(_RouteReady(
                    token,
                    generation,
                    None,
                    revision,
                    install_immediately,
                    failure if isinstance(failure, str) else "unreachable",
                ))
                return
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
            travel = self._mode or "walking"
            primary, secondary = _ROUTE_FAILURE_COPY.get(
                ready.failure or "rejected",
                ("Route unavailable", "Try again or change destination"),
            )
            if ready.failure == "no_route":
                primary = f"No {travel} route from here"
            await self._send_view(NavigationHudView(
                view="error",
                primary=primary,
                secondary=secondary,
                hint="tap/say retry · swipe cancel",
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
            hint="tap/say start · swipe up/down to edit",
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
                hint="Directions begin when ready · say stop",
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
            hint="tap/say retry · swipe cancel",
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
            hint="Old directions paused · say stop",
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


def _candidate_option(
    candidate: NavigationCandidate,
    *,
    proximity: NavigationPoint | None = None,
) -> str:
    distance = (
        f" · about {_format_distance(_candidate_distance(candidate, proximity))}"
        if proximity is not None
        else ""
    )
    value = f"{candidate.label}{distance} · {candidate.address}"
    return value if len(value) <= 64 else f"{value[:61].rstrip()}..."


def _destination_search(query: str) -> _DestinationSearch:
    """Separate place words from an explicit current-location modifier."""
    text = " ".join(query.strip().rstrip(".!?").split())
    nearby = False
    if _NEARBY_PREFIX.match(text) is not None:
        text = _NEARBY_PREFIX.sub("", text, count=1)
        nearby = True
    if _NEARBY_SUFFIX.search(text) is not None:
        text = _NEARBY_SUFFIX.sub("", text, count=1)
        nearby = True
    text = text.strip(" ,")
    return _DestinationSearch(text=text or query.strip(), nearby=nearby)


def _navigation_correction(text: str) -> str | None:
    request = " ".join(text.strip().rstrip(".!?").split())
    match = _CORRECTION_PREFIX.fullmatch(request)
    if match is None:
        return None
    destination = re.sub(r",\s*(?:the\s+)?", " ", match.group(1))
    destination = " ".join(destination.split()).strip(" ,")
    return destination or None


def _candidate_distance(
    candidate: NavigationCandidate,
    proximity: NavigationPoint,
) -> float:
    if (
        candidate.distance_m is not None
        and math.isfinite(candidate.distance_m)
        and candidate.distance_m >= 0
    ):
        return candidate.distance_m
    origin_lat = math.radians(proximity.latitude)
    target_lat = math.radians(candidate.point.latitude)
    delta_lat = target_lat - origin_lat
    delta_lon = math.radians(candidate.point.longitude - proximity.longitude)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(origin_lat) * math.cos(target_lat) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(haversine)))


def _format_distance(distance_m: float) -> str:
    if distance_m < 1_000:
        return f"{round(distance_m / 10) * 10:.0f} m"
    return f"{distance_m / 1_000:.1f} km"


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
                    hint="say cancel · tap or swipe close",
                    interaction="stationary",
                ),
            ))
            await self._settled.wait()
        finally:
            await self._transport.send(NavigationStop(session_id=self._session_id))

    def cancel(self) -> None:
        self._settled.set()

    def consume_transcript(self, text: str) -> bool:
        if _normalise_navigation_voice(text) not in _VOICE_CANCEL:
            return False
        self.cancel()
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
